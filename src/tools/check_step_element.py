"""
check_step_element - what a test step's element really is on the live page, and why a step on it fails.

A failing step was re-diagnosed from its error text alone, run after run: a timeout was put down to
the page, then to the framework, while the step's selector pointed at another field behind an open
dialog the whole time. Looking at the element as the step meets it - signed in, with the dialog
open - answers in one call what three reruns did not: does the selector find one element, is it the
element the step means, is something covering it, is it disabled, and which element on the page
fits instead.
"""

from playwright.async_api import async_playwright

from src.tools.browser_setup import ensure_chromium, is_missing_browser_error
from src.tools.crawl_url import _dedup_key, _dismiss_overlays, _extract_locators, _validate_locators
from src.tools.heal_locator import _kind_of, _login, _similarity
from src.tools.open_by_clicking import open_by_clicking as _open_by_clicking
from src.tools.url_guard import validate_public_http_url

MAX_SUGGESTIONS = 3

# Words that say what kind of element, or where it is - not which one.
_CONTEXT_WORDS = {"field", "input", "box", "textbox", "button", "link", "dropdown", "select", "checkbox", "dialog",
                  "modal", "popup", "page", "form", "menu", "the", "in", "on", "of", "for", "and", "a", "an", "inside",
                  "element", "text", "area"}
_KIND_WORDS = {"TEXTBOX": ("field", "input", "box", "textbox", "textarea"), "BUTTON": ("button",),
               "HYPERLINK": ("link",), "DROP_DOWN": ("dropdown", "select", "combobox"), "CHECK_BOX": ("checkbox",)}


def _key_words(intent: str, context: str = None) -> set:
    """The words of an intent that pick out the element: not its kind, and not the dialog it is in."""
    around = set((context or "").lower().split())
    return {w for w in intent.lower().replace('"', " ").split()
            if len(w) > 2 and w not in _CONTEXT_WORDS and w not in around}


def _wanted_kind(intent: str):
    words = set(intent.lower().split())
    return next((kind for kind, names in _KIND_WORDS.items() if words & set(names)), None)


def _matches(words: set, shown: str) -> float:
    if not words:
        return 0.0
    shown = shown.lower()
    return sum(1 for w in words if w in shown) / len(words)

# Everything about the element a person would see, plus what blocks it: whether it is in an open
# dialog, whether something else sits on top of its centre, and which framework renders the page.
_INSPECT_JS = r"""([selector, isXpath]) => {
    const all = isXpath
        ? (() => { const r = document.evaluate(selector, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                   const out = []; for (let i = 0; i < r.snapshotLength; i++) out.push(r.snapshotItem(i)); return out; })()
        : [...document.querySelectorAll(selector)];
    const clean = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 100);
    const describe = el => {
        if (!el) return null;
        const r = el.getBoundingClientRect(), style = getComputedStyle(el);
        return {
            tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || null,
            text: clean(el.innerText || el.value), label: el.labels && el.labels[0] ? clean(el.labels[0].textContent) : null,
            placeholder: el.getAttribute('placeholder'), ariaLabel: el.getAttribute('aria-label'), id: el.id || null,
            visible: r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none',
            disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
            inOpenDialog: !!el.closest('[role=dialog],[role=alertdialog],[aria-modal="true"],dialog[open]'),
        };
    };
    const el = all[0];
    let coveredBy = null;
    if (el) {
        const r = el.getBoundingClientRect();
        const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        if (top && top !== el && !el.contains(top) && !top.contains(el)) {
            coveredBy = { tag: top.tagName.toLowerCase(), text: clean(top.innerText).slice(0, 60),
                          classes: (typeof top.className === 'string' ? top.className : '').slice(0, 120) };
        }
    }
    const dialog = [...document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal="true"],dialog[open]')]
        .find(d => d.getBoundingClientRect().width > 0);
    const keys = el ? Object.keys(el) : [];
    const react = keys.some(k => k.startsWith('__react')) || !!document.querySelector('[data-reactroot]')
        || [...document.querySelectorAll('body *')].slice(0, 300).some(n => Object.keys(n).some(k => k.startsWith('__react')));
    const reactNativeWeb = !!document.querySelector('[class*="css-view-"], [class*="r-"][dir], #root [data-rnw]');
    return {
        count: all.length, element: describe(el), coveredBy,
        dialogOpen: dialog ? clean(dialog.querySelector('h1,h2,h3,[role=heading]')?.innerText || dialog.innerText).slice(0, 60) : null,
        framework: react ? (reactNativeWeb ? 'React Native Web' : 'React') : null,
    };
}"""


def _verdict(found: dict, intent: str) -> list[str]:
    """Plain findings, most decisive first."""
    out = []
    count, el = found.get("count", 0), found.get("element")
    if count == 0:
        out.append("The selector finds no element on this page as the step meets it.")
        return out
    if count > 1:
        out.append(f"The selector matches {count} elements; the step acts on the first one, which may be the wrong one.")
    if el and not el.get("visible"):
        out.append("The element is in the page but not visible.")
    if found.get("dialogOpen") and el and not el.get("inOpenDialog"):
        out.append(f'A dialog ("{found["dialogOpen"]}") is open and this element is behind it, so the step cannot reach it.')
    if found.get("coveredBy"):
        c = found["coveredBy"]
        out.append(f"Another element covers it (a {c['tag']}{' showing ' + repr(c['text']) if c['text'] else ''}"
                   f"{', classes ' + c['classes'] if c['classes'] else ''}), so a click or typing lands on that instead.")
    if el and el.get("disabled"):
        out.append("The element is disabled.")
    if intent and el:
        shown = " ".join(str(el.get(k) or "") for k in ("text", "label", "placeholder", "ariaLabel", "id"))
        words = _key_words(intent, found.get("dialogOpen"))
        if shown.strip() and words and _matches(words, shown) == 0:
            out.append(f'The element shows "{shown.strip()[:80]}", which is not "{intent}" - the step acts on the '
                       "wrong element.")
    if not out:
        out.append("The element looks reachable: one visible, enabled, uncovered match.")
    if found.get("framework"):
        out.append(f"The page is built with {found['framework']}: type into a field with a real Enter step - setting its "
                   "value from JavaScript does not reach the app's state, so the app acts as if nothing was typed.")
    return out


async def check_step_element(url: str, locator_value: str = None, locate_by: str = "css", intent: str = None,
                             credentials: dict = None, login_url: str = None, open_by_clicking: list = None,
                             hosted: bool = False) -> dict:
    """
    Opens `url` as the step meets it - signed in via `login_url`, with `open_by_clicking` opened - and
    reports what `locator_value` resolves to, what blocks it, and which elements fit `intent` instead.
    Writes nothing.
    """
    if hosted:
        blocked = await validate_public_http_url(url) or (login_url and await validate_public_http_url(login_url))
        if blocked:
            return {"error": blocked}

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
            signed_in = None
            if credentials:
                await page.goto(login_url or url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.wait_for_selector('input[type="password"]', timeout=15_000)
                    await _login(page, credentials)
                    signed_in = True
                except Exception as exc:
                    signed_in = f"sign-in did not complete: {type(exc).__name__}"
            if _dedup_key(page.url) != _dedup_key(url):
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            await page.wait_for_timeout(1_000)
            overlays = await _dismiss_overlays(page)
            opening = await _open_by_clicking(page, open_by_clicking) if open_by_clicking else {}

            found = None
            if locator_value:
                try:
                    found = await page.evaluate(_INSPECT_JS, [locator_value, (locate_by or "").lower() == "xpath"])
                except Exception as exc:
                    found = {"count": 0, "error": f"the selector is invalid: {exc}"}

            suggestions = []
            if intent:
                candidates = await _validate_locators(page, await _extract_locators(page))
                dialog_open = bool(found and found.get("dialogOpen")) or bool(opening.get("opened_by_clicking"))
                words = _key_words(intent, found.get("dialogOpen") if found else None)
                kind = _wanted_kind(intent)
                scored = []
                for loc in candidates:
                    if kind and _kind_of(loc) != kind:
                        continue
                    shown = " ".join(str(loc.get(k) or "") for k in ("text", "label", "placeholder", "ariaLabel"))
                    score = _matches(words, shown) * 0.7 + _similarity(intent, shown) * 0.3
                    if dialog_open and loc.get("inDialog"):
                        score += 0.2
                    if score >= 0.35:
                        best = (loc.get("strategies") or [{}])[0]
                        scored.append({"score": round(min(score, 1.0), 2), "shows": shown.strip()[:80], "tag": loc.get("tag"),
                                       "locateBy": best.get("locateBy"), "locatorValue": best.get("locatorValue"),
                                       "fragile": bool(loc.get("fragile"))})
                scored.sort(key=lambda s: s["score"], reverse=True)
                suggestions = scored[:MAX_SUGGESTIONS]
            current_url = page.url
        finally:
            await browser.close()

    result = {
        "url": current_url,
        "signed_in": signed_in,
        **opening,
        **({"overlays_closed_on_arrival": overlays} if overlays else {}),
    }
    if found is not None:
        result["selector"] = {"locateBy": locate_by, "locatorValue": locator_value}
        result["resolves_to"] = found
        result["findings"] = _verdict(found, intent or "")
    if intent:
        result["elements_matching_intent"] = suggestions
        if not suggestions:
            result["intent_note"] = (f'Nothing on the page as it stands looks like "{intent}". If it is inside a dialog or '
                                     "menu, pass open_by_clicking with the texts that open it.")
    return result
