from difflib import SequenceMatcher

from playwright.async_api import async_playwright

from src.tools.browser_setup import ensure_chromium, is_missing_browser_error
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
    fields = (element.get("text", ""), element.get("ariaLabel", ""), element.get("placeholder", ""), element.get("name", ""))
    return max((_similarity(locator_name, f) for f in fields), default=0.0)


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


async def heal_locator(asset_client, locator_id: str, website_id: str, credentials: dict = None, hosted: bool = False) -> dict:
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
        blocked = await validate_public_http_url(page_url)
        if blocked:
            return {"error": blocked}

    locator_name = locator.get("locatorName", "")

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
                    await _login(page, credentials)
                except Exception:
                    pass

            elements = await page.evaluate(_CANDIDATE_STRATEGIES_JS)

            scored = []
            for element in elements:
                match_score = _match_score(locator_name, element)
                if match_score <= MIN_MATCH_SCORE:
                    continue
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
    }
