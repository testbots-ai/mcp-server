"""
Impact analysis for a locator: which test scripts and Common Functions actually reference it.

A locator is shared, and nothing in the editor says so. A login element referenced by both a
"Login as sanity user" Common Function and a standalone Login script looks identical from either
side, so a fix applied in one place reads as complete while the other caller keeps failing — and
the reverse mistake is worse: a locator edited to suit one script silently changes every other
script that points at the same id. Neither direction is visible without walking every script's
steps, which is why this is a tool and not a manual cross-reference.
"""

import asyncio

# The placeholder names a step uses for an element reference. `templateTitle` spells it
# "ui-locator"; the entity field is `uiLocator`. Both appear as parameter keys in live data.
_LOCATOR_KEYS = ("ui-locator", "uiLocator")

_MAX_CONCURRENT_FETCHES = 8


def _locator_ids_in_step(step: dict):
    """Every locatorId a single step references, including any nested sub-steps (loop bodies)."""
    found = set()
    if not isinstance(step, dict):
        return found

    for param in step.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        if param.get("key") not in _LOCATOR_KEYS:
            continue
        value = param.get("value")
        # A ui-locator value is {"locatorId": ...}; the server enriches the rest server-side.
        if isinstance(value, dict) and value.get("locatorId"):
            found.add(value["locatorId"])

    for sub in step.get("subTestSteps") or []:
        found |= _locator_ids_in_step(sub)
    return found


def _matches(steps, locator_id: str):
    """The steps of one document that reference `locator_id`, as compact hits."""
    hits = []
    for step in steps or []:
        if locator_id in _locator_ids_in_step(step):
            hits.append({
                "sequence": step.get("sequence"),
                "testStepId": step.get("testStepId"),
                "testStepTitle": step.get("testStepTitle") or step.get("templateTitle"),
            })
    return hits


async def find_locator_usage(clients, locator_id: str, include_common_functions: bool = True) -> dict:
    """
    Walk every test script (and, by default, every Common Function) in the project and report
    which ones reference `locator_id`.

    The list endpoints return document summaries without `testSteps`, so each candidate has to be
    fetched to be searched. That is the cost of the answer — it is bounded concurrency over the
    project, not a cheap lookup, so call it when deciding the blast radius of a locator change,
    not on every step edit.
    """
    if not locator_id:
        return {"error": "locator_id is required."}

    try:
        scripts = await clients.test_mgmt.list_test_scripts()
    except Exception as e:
        return {"error": f"Could not list test scripts: {e}"}
    if not isinstance(scripts, list):
        scripts = []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)

    async def scan_script(summary):
        script_id = summary.get("testScriptId") or summary.get("id")
        if not script_id:
            return None
        async with semaphore:
            try:
                full = await clients.test_mgmt.get_test_script(script_id)
            except Exception as e:
                # One unreadable script must not sink the whole report — name it and move on,
                # otherwise the caller gets an error where a partial answer was available.
                return {"kind": "test_script", "id": script_id,
                        "name": summary.get("name"), "error": str(e)}
        hits = _matches((full or {}).get("testSteps"), locator_id)
        if not hits:
            return None
        return {
            "kind": "test_script",
            "id": script_id,
            "name": (full or {}).get("name") or summary.get("name"),
            "branch": (full or {}).get("currentBranchName"),
            "steps": hits,
        }

    results = await asyncio.gather(*(scan_script(s) for s in scripts if isinstance(s, dict)))
    usages = [r for r in results if r and "error" not in r]
    unreadable = [r for r in results if r and "error" in r]

    if include_common_functions:
        try:
            functions = await clients.asset.list_common_functions()
        except Exception as e:
            unreadable.append({"kind": "common_function", "error": f"Could not list: {e}"})
            functions = []
        if not isinstance(functions, list):
            functions = []

        async def scan_function(summary):
            function_id = summary.get("commonFunctionId") or summary.get("id")
            if not function_id:
                return None
            async with semaphore:
                try:
                    full = await clients.asset.get_common_function(function_id)
                except Exception as e:
                    return {"kind": "common_function", "id": function_id,
                            "name": summary.get("name"), "error": str(e)}
            hits = _matches((full or {}).get("testSteps"), locator_id)
            if not hits:
                return None
            return {
                "kind": "common_function",
                "id": function_id,
                "name": (full or {}).get("name") or summary.get("name"),
                "steps": hits,
            }

        fn_results = await asyncio.gather(*(scan_function(f) for f in functions if isinstance(f, dict)))
        usages += [r for r in fn_results if r and "error" not in r]
        unreadable += [r for r in fn_results if r and "error" in r]

    out = {
        "locator_id": locator_id,
        "usage_count": len(usages),
        "scripts_scanned": len(scripts),
        "usages": usages,
    }
    if unreadable:
        # Say what was NOT searched. A silent gap here turns "used in 1 place" into a wrong
        # all-clear, which is the exact failure this tool exists to prevent.
        out["not_searched"] = unreadable
        out["warning"] = (
            f"{len(unreadable)} document{'' if len(unreadable)==1 else 's'} could not be read and "
            f"{'was' if len(unreadable)==1 else 'were'} NOT searched — this result may be incomplete."
        )
    if not usages and not unreadable:
        out["note"] = (
            "No script or Common Function references this locator. If a run is still failing on "
            "it, the failing step may carry its own embedded locator snapshot rather than a "
            "reference — check the step itself."
        )
    return out
