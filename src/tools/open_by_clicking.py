"""
Clicks through to an element that is not on the page until something is opened.

A field inside a dialog - the name field of a "New Test Bot" dialog, say - is not in the DOM when
the page loads, so neither crawl_url nor heal_locator could ever capture it: the crawl returned the
page behind the dialog, and the model saved a selector from another page in its place. Clicking the
controls that open it first, by their visible text, puts the element on the page to be captured.
"""

AFTER_CLICK_WAIT_MS = 1_500
CLICK_TIMEOUT_MS = 5_000


FIND_ATTEMPTS = 10
FIND_RETRY_MS = 500


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


async def open_by_clicking(page, texts) -> dict:
    """
    Clicks each control in `texts`, in order, waiting after each one. Stops at the first that
    cannot be found or clicked, since the later ones are usually inside what it would have opened.
    """
    opened, not_found = [], []
    for text in [t.strip() for t in (texts or []) if isinstance(t, str) and t.strip()]:
        target = await _visible_target(page, text)
        if target is None:
            not_found.append(text)
            break
        try:
            await target.click(timeout=CLICK_TIMEOUT_MS)
        except Exception:
            not_found.append(text)
            break
        opened.append(text)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass
        await page.wait_for_timeout(AFTER_CLICK_WAIT_MS)
    result = {"opened_by_clicking": opened}
    if not_found:
        result["could_not_open"] = not_found
        result["open_note"] = (f'Could not find or click "{not_found[0]}" - check its exact visible text. '
                               "Elements behind it were not captured.")
    return result
