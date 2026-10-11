"""
Clicks through to an element that is not on the page until something is opened.

A field inside a dialog - the name field of a "New Test Bot" dialog, say - is not in the DOM when
the page loads, so neither crawl_url nor heal_locator could ever capture it: the crawl returned the
page behind the dialog, and the model saved a selector from another page in its place. Clicking the
controls that open it first, by their visible text, puts the element on the page to be captured.
"""

import re

AFTER_CLICK_WAIT_MS = 1_500
# Controls that change or destroy data - never clicked to reveal an element. Capturing a Delete
# button needs no click: the crawl reads it from the page. A click on one deletes the item, and on
# some apps with no confirmation at all.
NEVER_CLICK = re.compile(
    r"\b(delete|remove|archive|discard|trash|erase|purge|wipe|reset|clear( all)?|log\s?out|sign\s?out|"
    r"deactivate|disable|disconnect|revoke|uninstall|unsubscribe|cancel (subscription|plan|account)|"
    r"run|execute|re-?run|stop|send|submit|publish|deploy|merge|pay|purchase|buy|checkout|"
    r"confirm|approve|reject|transfer)\b", re.IGNORECASE)
CLICK_TIMEOUT_MS = 5_000
NEW_TAB_WAIT_STEPS = 20  # x 500 ms: how long a target="_blank" link is given to open its tab


FIND_ATTEMPTS = 10
FIND_RETRY_MS = 500


async def _follow_new_tab(page, new: list, hosted: bool):
    """The address of a tab the click opened, now loaded in `page`, or None when none opened."""
    from src.tools.url_guard import validate_public_http_url

    new = [tab for tab in new if tab is not page]
    if not new:
        return None
    tab = new[-1]
    try:
        await tab.wait_for_load_state("domcontentloaded", timeout=10_000)
    except Exception:
        pass
    for _ in range(20):  # a tab opened by a link starts at about:blank before its address arrives
        if tab.url and tab.url != "about:blank":
            break
        await tab.wait_for_timeout(250)
    url = tab.url
    for extra in new:
        try:
            await extra.close()
        except Exception:
            pass
    if not url or url == "about:blank" or (hosted and await validate_public_http_url(url)):
        return None
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        await page.wait_for_timeout(AFTER_CLICK_WAIT_MS)
    except Exception:
        return None
    return url


async def _visible_target(page, text: str):
    """
    The first visible control named `text` - exactly if one is, else containing it, since an icon
    such as "+" is often part of a button's name - or None. Retried briefly: a list page renders its
    toolbar after the data arrives.
    """
    for _ in range(FIND_ATTEMPTS):
        target = await _first_visible(page, text)
        if target is not None:
            return target
        await page.wait_for_timeout(FIND_RETRY_MS)
    return None


async def _first_visible(page, text: str):
    candidates = [page.get_by_role(role, name=text, exact=True) for role in ("button", "link", "menuitem", "tab")]
    candidates.append(page.get_by_text(text, exact=True))
    candidates += [page.get_by_role(role, name=text) for role in ("button", "link", "menuitem", "tab")]
    for locator in candidates:
        try:
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 5)):
            target = locator.nth(index)
            try:
                if await target.is_visible():
                    return target
            except Exception:
                continue
    return None


async def open_by_clicking(page, texts, hosted: bool = False) -> dict:
    """
    Clicks each control in `texts`, in order, waiting after each one. Stops at the first that
    cannot be found or clicked, since the later ones are usually inside what it would have opened.

    A click that opens a new tab - a marketing page's "Sign In" that opens the app's sign-in page
    with target="_blank" - is followed: the new tab's address is loaded in this page and the tab is
    closed, so the caller captures what the click opened rather than the page left behind.
    """
    opened, not_found, new_tabs = [], [], []
    for text in [t.strip() for t in (texts or []) if isinstance(t, str) and t.strip()]:
        if NEVER_CLICK.search(text):
            return {"opened_by_clicking": opened, "could_not_open": [text], "refused": text,
                    "open_note": (f'"{text}" was not clicked: it changes or deletes data. Its element is '
                                  "captured without clicking it - crawl the page that shows it, with "
                                  "open_by_clicking only for what opens that page or dialog.")}
        target = await _visible_target(page, text)
        if target is None:
            not_found.append(text)
            break
        # Tabs are collected as they open: a tab a link opens can appear well after the click returns,
        # so a list of tabs taken afterwards is not reliable.
        new_tabs_opened = []

        def collect(tab):  # a plain function: Playwright cannot take a built-in method as a listener
            new_tabs_opened.append(tab)

        page.context.on("page", collect)
        try:
            # A link that opens a new tab can take a few seconds to do it; the wait below is for that.
            opens_tab = await target.evaluate("el => !!el.closest('a[target=\"_blank\"]')")
        except Exception:
            opens_tab = False
        try:
            await target.click(timeout=CLICK_TIMEOUT_MS)
        except Exception:
            page.context.remove_listener("page", collect)
            not_found.append(text)
            break
        opened.append(text)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        await page.wait_for_timeout(AFTER_CLICK_WAIT_MS)
        for _ in range(NEW_TAB_WAIT_STEPS if opens_tab else 0):
            if new_tabs_opened:
                break
            await page.wait_for_timeout(500)
        page.context.remove_listener("page", collect)
        followed = await _follow_new_tab(page, new_tabs_opened, hosted)
        if followed:
            new_tabs.append(followed)
    result = {"opened_by_clicking": opened}
    if new_tabs:
        result["opened_in_new_tab"] = new_tabs
    if not_found:
        result["could_not_open"] = not_found
        result["open_note"] = (f'Could not find or click "{not_found[0]}" - check its exact visible text. '
                               "Elements behind it were not captured.")
    return result
