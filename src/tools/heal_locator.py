from difflib import SequenceMatcher

from playwright.async_api import async_playwright

from src.tools.browser_setup import ensure_chromium, is_missing_browser_error
from src.tools.crawl_url import _dedup_key, _dismiss_overlays
from src.tools.open_by_clicking import open_by_clicking as _open_by_clicking
from src.tools.url_guard import validate_public_http_url

MAX_CANDIDATES = 3
# Below this, a candidate element almost certainly isn't the same one the broken locator used to
# point at — better to report "nothing found" than propose a wrong fix.
MIN_MATCH_SCORE = 0.3

# Ranked in the same priority order as the browser extension's already-validated locator scorer
# (id > data-testid > aria-label > name > structural fallback) — every candidate is returned, not
# just the first match, so the caller can pick whichever one actually resolves uniquely below.
_CANDIDATE_STRATEGIES_JS = """() => {
    const results = [];
    const interactable = document.querySelectorAll(
        'a, button, input, select, textarea, [role="button"], [role="link"], [role="menuitem"], [onclick]'
    );

    interactable.forEach((el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return;

        const candidates = [];
        if (el.id) candidates.push({ locateBy: 'css', locatorValue: `#${el.id}` });
        if (el.getAttribute('data-testid')) {
            candidates.push({ locateBy: 'css', locatorValue: `[data-testid="${el.getAttribute('data-testid')}"]` });
        }
        if (el.getAttribute('aria-label')) {
            candidates.push({ locateBy: 'css', locatorValue: `[aria-label="${el.getAttribute('aria-label')}"]` });
        }
        if (el.getAttribute('name')) {
            candidates.push({ locateBy: 'css', locatorValue: `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]` });
        }
        if (el.getAttribute('placeholder')) {
            candidates.push({ locateBy: 'css', locatorValue: `${el.tagName.toLowerCase()}[placeholder="${el.getAttribute('placeholder')}"]` });
        }
        if (typeof el.className === 'string' && el.className.trim()) {
            // CSS.escape per class: an unescaped Tailwind arbitrary value like mt-[0.5px]
            // makes the selector invalid rather than merely unmatched, so the candidate it
            // produces can never heal anything.
            const cls = el.className.trim().split(/\\s+/).filter(Boolean).map(function (c) { return CSS.escape(c); }).join('.');
            if (cls) candidates.push({ locateBy: 'css', locatorValue: `${el.tagName.toLowerCase()}.${cls}` });
        }

        results.push({
            candidates: candidates,
            text: (el.innerText || el.value || '').trim().slice(0, 100),
            ariaLabel: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
            name: el.getAttribute('name') || '',
            label: ((el.labels && el.labels[0] && el.labels[0].innerText) || '').trim().slice(0, 100),
            tag: el.tagName.toLowerCase(),
            type: (el.getAttribute('type') || '').toLowerCase(),
            role: (el.getAttribute('role') || '').toLowerCase(),
            inDialog: !!el.closest('[role=dialog],[role=alertdialog],[aria-modal="true"],dialog[open]'),
        });
    });
    return results;
}"""


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _match_score(locator_name: str, element: dict) -> float:
    # Heuristic-only matching (no LLM) for this slice, matching how competitors' own
    # self-healing engines phase in a rule-based pass before an AI-backed one.
    fields = (element.get("text", ""), element.get("ariaLabel", ""), element.get("placeholder", ""),
              element.get("name", ""), element.get("label", ""))
    return max((_similarity(locator_name, f) for f in fields), default=0.0)


_TEXT_INPUT_TYPES = {"", "text", "email", "password", "search", "tel", "url", "number"}


def _kind_of(element: dict) -> str:
    tag, kind, role = element.get("tag") or "", (element.get("type") or "").lower(), (element.get("role") or "").lower()
    if tag == "textarea" or (tag == "input" and kind in _TEXT_INPUT_TYPES):
        return "TEXTBOX"
    if tag == "select" or role in ("combobox", "listbox"):
        return "DROP_DOWN"
    if tag == "input" and kind == "checkbox":
        return "CHECK_BOX"
    if tag == "input" and kind == "radio":
        return "RADIO_BUTTON"
    if tag == "a" or role == "link":
        return "HYPERLINK"
    if tag == "button" or role == "button" or (tag == "input" and kind in ("submit", "button", "reset")):
        return "BUTTON"
    return "OTHER"


def _same_kind(locator_type: str, element: dict) -> bool:
    """
    Only an element of the broken one's kind can replace it. Ranked by name alone, a heal of an
    account menu proposed the page's help button, and it was applied.
    """
    wanted = (locator_type or "").upper()
    if wanted not in ("TEXTBOX", "DROP_DOWN", "CHECK_BOX", "RADIO_BUTTON", "HYPERLINK", "BUTTON"):
        return True
    found = _kind_of(element)
    # A link and a button are often the same control to a user; either may replace the other.
    if {wanted, found} <= {"HYPERLINK", "BUTTON"}:
        return True
    return found == wanted


async def _login(page, credentials: dict) -> None:
    # Mirrors crawl_url._crawl's login flow exactly (same SPA post-login redirect race applies
    # here) — kept as a local copy rather than a shared import since crawl_url's version is
    # scoped to a throwaway login_page/context it manages itself.
    password_field = page.locator('input[type="password"]').first
    form = password_field.locator("xpath=ancestor::form[1]")
    await form.locator(
        'input[type="email"], input[name*="user"], input[name*="email"]'
    ).first.fill(credentials.get("username", ""))
    await password_field.fill(credentials.get("password", ""))
    start_url = page.url
    await form.locator('button[type="submit"], input[type="submit"]').first.click()
    try:
        await page.wait_for_url(lambda u: u != start_url, timeout=15_000)
    except Exception:
        pass
    await page.wait_for_load_state("networkidle", timeout=15_000)


async def heal_locator(asset_client, locator_id: str, website_id: str, credentials: dict = None, hosted: bool = False,
                       open_by_clicking: list = None, login_url: str = None) -> dict:
    """
    Propose-only: re-crawls the broken locator's live page and returns ranked replacement
    selector candidates. Never writes anything — apply_locator_fix (a separate tool, backed by
    AssetClient.apply_locator_strategy) is the only path that changes a stored locator.
    """
    page_doc = await asset_client.get_page_by_locator_id(website_id, locator_id)
    locator = next((l for l in (page_doc.get("locators") or []) if l.get("locatorId") == locator_id), None)
    if locator is None:
        return {"error": f"Locator {locator_id} was not found on its own page — it may already have been deleted or archived."}

    page_url = page_doc.get("pageUrl")
    if not page_url:
        return {"error": "This page has no recorded URL to re-crawl — cannot propose a replacement selector."}

    if hosted:
        blocked = await validate_public_http_url(page_url) or (login_url and await validate_public_http_url(login_url))
        if blocked:
            return {"error": blocked}

    locator_name = locator.get("locatorName", "")
    locator_type = locator.get("locatorType", "")
    opening = {}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:
            if not is_missing_browser_error(e):
                raise
            failure = await ensure_chromium(hosted)
            if failure:
                return {"error": failure}
            browser = await p.chromium.launch(headless=True)

        try:
            page = await browser.new_page()
            await page.goto(page_url, wait_until="networkidle", timeout=30_000)
            if credentials:
                try:
                    if login_url:
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=30_000)
                        await page.wait_for_selector('input[type="password"]', timeout=15_000)
                    await _login(page, credentials)
                except Exception:
                    pass
                # Signing in usually lands on the app's home page, not the element's page.
                if _dedup_key(page.url) != _dedup_key(page_url):
                    try:
                        await page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
            if open_by_clicking:
                await _dismiss_overlays(page)
                opening = await _open_by_clicking(page, open_by_clicking)

            elements = await page.evaluate(_CANDIDATE_STRATEGIES_JS)

            scored = []
            for element in elements:
                if not _same_kind(locator_type, element):
                    continue
                match_score = _match_score(locator_name, element)
                if match_score <= MIN_MATCH_SCORE:
                    continue
                # What was opened to reach it is where the element is: prefer it to the page behind.
                if opening.get("opened_by_clicking") and element.get("inDialog"):
                    match_score = min(1.0, match_score + 0.15)
                for candidate in element["candidates"]:
                    try:
                        count = await page.locator(candidate["locatorValue"]).count()
                    except Exception:
                        continue
                    # A selector matching more than one element is ambiguous, not a fix — the
                    # same specificity check crawl_url's validation skips (it only checks
                    # count() > 0). Self-healing needs count() == 1 or it just trades one broken
                    # locator for one that resolves to the wrong element some of the time.
                    if count != 1:
                        continue
                    scored.append({
                        "locateBy": candidate["locateBy"],
                        "locatorValue": candidate["locatorValue"],
                        "confidence": round(match_score, 2),
                    })
        finally:
            await browser.close()

    scored.sort(key=lambda c: c["confidence"], reverse=True)
    top = scored[:MAX_CANDIDATES]

    return {
        "locator_id": locator_id,
        "locator_name": locator_name,
        "page_url": page_url,
        "current_strategies": locator.get("locationStrategies") or [],
        "candidates": top,
        "found": bool(top),
        **opening,
    }
