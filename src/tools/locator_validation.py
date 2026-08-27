"""
Resolve the locators a set of test steps references, before the steps are written.

A step's element reference is an opaque id: `{"locatorId": "68e5df70-..."}`. Nothing in the call,
the response, or the stored document says which element that actually is, so pasting the id of a
real-but-unrelated element is accepted silently — the server renders
`(Pending) uiLocator not found` for one that does not exist, and a perfectly normal-looking step
title for one that exists on the wrong page. Both surface as a failed run much later.

Two checks, split by how certain each one is:

- **An id that resolves to nothing is refused.** There is no legitimate reading of it, and today
  it produces a placeholder step rather than an error.
- **An id that resolves is echoed back** with its name and page. This is the one that catches the
  real mistake: a wait step meant for "Add Custom Properties Button" that was given the id of
  "Generate API Token Button" is valid, same website, and wrong — visible instantly once the call
  answers with the name, and invisible for three more steps without it.

Deliberately NOT a website/page match rule. A script legitimately spans pages, and the two
elements in the case above sit on different pages of the same application, so a containment check
would both miss that error and reject ordinary navigation flows.
"""

import asyncio

_LOCATOR_KEYS = ("ui-locator", "uiLocator")
_MAX_CONCURRENT_LOOKUPS = 8


def locator_refs_in_steps(steps):
    """
    Every (sequence, locatorId) a step list references, sub-steps included. Sequence is whatever
    the caller sent — steps being added are often unnumbered, so it is only a label for messages.
    """
    refs = []

    def walk(step_list):
        for step in step_list or []:
            if not isinstance(step, dict):
                continue
            for param in step.get("parameters") or []:
                if not isinstance(param, dict) or param.get("key") not in _LOCATOR_KEYS:
                    continue
                value = param.get("value")
                if isinstance(value, dict) and value.get("locatorId"):
                    refs.append({
                        "sequence": step.get("sequence"),
                        "step_title": step.get("testStepTitle") or step.get("templateTitle"),
                        "locator_id": value["locatorId"],
                    })
            walk(step.get("subTestSteps"))

    walk(steps)
    return refs


async def resolve_step_locators(clients, website_id: str, steps) -> dict:
    """
    Look up every locator the steps reference against `website_id`.

    Returns {"resolved": [...], "unresolved": [...], "unverified": bool}. `unverified` is set when
    the lookup itself could not run — a transport error, or no website_id to look up against. In
    that case nothing is reported as unresolved, because a check that cannot run must never become
    the failure; same discipline as _preflight_execution_configuration.
    """
    refs = locator_refs_in_steps(steps)
    if not refs:
        return {"resolved": [], "unresolved": [], "unverified": False}
    if not website_id:
        return {"resolved": [], "unresolved": [], "unverified": True}

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)
    # One lookup per distinct id — a locator used by five steps is still one element.
    distinct = sorted({ref["locator_id"] for ref in refs})

    async def lookup(locator_id):
        async with semaphore:
            try:
                page = await clients.asset.get_page_by_locator_id(website_id, locator_id)
            except Exception as e:
                return locator_id, None, e
        return locator_id, page, None

    results = await asyncio.gather(*(lookup(lid) for lid in distinct))

    found, missing, errored = {}, set(), False
    for locator_id, page, error in results:
        if error is not None:
            # A 404 here is the endpoint saying "not on this website"; anything else is the check
            # failing rather than the locator being wrong.
            if getattr(error, "status_code", None) == 404 or "404" in str(error):
                missing.add(locator_id)
            else:
                errored = True
            continue
        if not isinstance(page, dict) or not page:
            missing.add(locator_id)
            continue
        name = None
        for loc in page.get("locators") or []:
            if isinstance(loc, dict) and (loc.get("locatorId") or loc.get("id")) == locator_id:
                name = loc.get("locatorName")
                break
        found[locator_id] = {
            "locator_name": name,
            "page_name": page.get("pageName"),
            "page_url": page.get("pageUrl"),
        }

    resolved, unresolved = [], []
    for ref in refs:
        detail = found.get(ref["locator_id"])
        entry = {"sequence": ref["sequence"], "step": ref["step_title"], "locator_id": ref["locator_id"]}
        if detail:
            entry.update(detail)
            resolved.append(entry)
        elif ref["locator_id"] in missing:
            unresolved.append(entry)

    return {"resolved": resolved, "unresolved": unresolved, "unverified": errored}


def refusal_for(unresolved, website_id: str) -> dict:
    """The error returned instead of writing steps that reference ids resolving to nothing."""
    listed = ", ".join(
        f"{u['locator_id']} (step {u['sequence'] if u['sequence'] is not None else '?'})"
        for u in unresolved
    )
    return {"error": (
        f"Nothing was written — {len(unresolved)} step locator "
        f"{'reference' if len(unresolved) == 1 else 'references'} could not be resolved on website "
        f"{website_id}: {listed}. A locatorId that does not resolve is stored anyway and renders as "
        f"'(Pending) uiLocator not found' at run time, so this is refused rather than written. "
        f"Confirm the id with get_page_by_url or add_locators, then retry."
    )}


def annotate(result, resolution: dict):
    """
    Attach what each locator actually IS to a successful write, so a valid-but-wrong id is visible
    at the call site instead of three steps later.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    if resolution.get("resolved"):
        result["resolved_locators"] = resolution["resolved"]
        result["check_this"] = (
            "Confirm each locator_name above is the element the step was meant to act on — a "
            "locatorId belonging to a different element is valid and silently wrong."
        )
    if resolution.get("unverified"):
        result["locator_check"] = "skipped — locator lookup was unavailable, ids were not verified"
    return result
