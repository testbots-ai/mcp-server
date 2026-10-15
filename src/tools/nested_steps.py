"""
A script's steps as a run executes them: pre-scripts, nested scripts, user test steps and loop or
condition blocks opened up in place, every step numbered the way the run report numbers it.

A step that runs others - "Run Pre-Script", a user test step (Common Function), a script called as
a step - holds none of their steps itself: the run fetches and executes them, and reports a failure
inside as "3.2" (step 2 of what step 3 runs). Reading the calling script alone therefore shows the
step that failed as a one-line "Run Pre-Script ..." with no element, and a diagnosis or fix built
from that reads the wrong step. Opening the nested steps here, in order, gives the steps that
actually ran and what each acted on.

Numbering follows UnifiedConsolidationService: a nested step is "<calling step's number>.<its own
sequenceId or sequence>"; a block's body steps already carry dotted sequenceIds ("1.1") in the
script itself.

Typed values in a password-like step are masked in the titles returned here - the same rule as the
credential summary on get_test_script: a working credential must not reach the transcript.
"""
import re

RUN_PRE_SCRIPT = "template-id-124"
_SCRIPT_KEYS = ("pre-script", "post-script")
_LOCATOR_KEYS = ("ui-locator", "uiLocator")
_SECRET = re.compile(r"(?i)pass(word|code)?|pwd|secret|\bpin\b|otp|token")
MAX_DEPTH = 4
MAX_FETCHES = 20
MASK = "********"


def nested_script_id(step: dict):
    """The script a step runs, in each of the shapes a Run Pre-Script step has been saved in."""
    if step.get("subScriptId"):
        return step["subScriptId"]
    test = ((step.get("params") or {}).get("test") or {})
    if test.get("testScriptId"):
        return test["testScriptId"]
    for param in step.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        value = param.get("value")
        scriptish = param.get("key") in _SCRIPT_KEYS or str(param.get("paramClass") or "").endswith("TestScriptValuePair")
        if scriptish and isinstance(value, dict) and value.get("testScriptId"):
            return value["testScriptId"]
    return None


def _is_user_test_step(step: dict) -> bool:
    template = step.get("templateId") or ""
    return bool(template) and not template.startswith("template-id-") and not nested_script_id(step)


def _element(step: dict):
    for param in step.get("parameters") or []:
        if isinstance(param, dict) and param.get("key") in _LOCATOR_KEYS and isinstance(param.get("value"), dict):
            value = param["value"]
            return {k: value.get(k) for k in ("locatorId", "locatorName", "locateBy", "locatorValue") if value.get(k)}
    return None


def _title(step: dict) -> str:
    title = str(step.get("testStepTitle") or step.get("templateTitle") or "")
    element = _element(step) or {}
    if _SECRET.search(title) or _SECRET.search(str(element.get("locatorName") or "")):
        for param in step.get("parameters") or []:
            value = param.get("value") if isinstance(param, dict) else None
            if isinstance(value, dict) and value.get("type") == 0 and value.get("value"):
                title = title.replace(str(value["value"]), MASK)
    return title


def _own_number(step: dict) -> str:
    return str(step.get("sequenceId") or step.get("sequence"))


class _Budget:
    def __init__(self):
        self.fetches = 0
        self.locators = {}


async def _resolve_element(clients, element: dict, website_id: str, budget: _Budget) -> dict:
    """
    A user test step's own steps keep only the element's id - the script endpoint fills in the
    selector for a script's steps, the Common Function endpoint does not. Without it the step reads
    as acting on nothing, so the selector is looked up from the element's page.
    """
    locator_id = element.get("locatorId")
    if element.get("locatorValue") or not locator_id or not website_id:
        return element
    if locator_id not in budget.locators:
        found = None
        try:
            page = await clients.asset.get_page_by_locator_id(website_id, locator_id)
            locator = next((l for l in (page or {}).get("locators") or [] if l.get("locatorId") == locator_id), None)
            if locator:
                strategies = locator.get("locationStrategies") or []
                chosen = next((st for st in strategies if st.get("selected")), strategies[0] if strategies else {})
                found = {"locatorName": locator.get("locatorName"), "locateBy": chosen.get("locateBy"),
                         "locatorValue": chosen.get("locatorValue")}
        except Exception:  # the step is still listed; it just shows no selector
            found = None
        budget.locators[locator_id] = found
    found = budget.locators[locator_id]
    return {**element, **{k: v for k, v in (found or {}).items() if v}}


async def expand_steps(clients, steps: list, branch: str = None, prefix: str = None, inside: dict = None,
                       depth: int = 0, seen: frozenset = frozenset(), budget: _Budget = None,
                       website_id: str = None) -> list:
    """
    Every step in run order. Each entry: number, title, templateId, element (when it acts on one),
    inside (the pre-script, script or user test step it belongs to, absent for the script's own
    steps), and for a step that runs others, runs = {kind, id, name}.
    """
    budget = budget or _Budget()
    out = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        number = _own_number(step) if prefix is None else f"{prefix}.{_own_number(step)}"
        entry = {"number": number, "title": _title(step), "templateId": step.get("templateId")}
        element = _element(step)
        if element:
            entry["element"] = await _resolve_element(clients, element, website_id, budget)
        if inside:
            entry["inside"] = inside
        if step.get("skip"):
            entry["skipped"] = True
        out.append(entry)

        children, runs, child_website = None, None, website_id
        script_id = nested_script_id(step)
        if step.get("subTestSteps"):
            children = step["subTestSteps"]
            runs = {"kind": "pre-script" if step.get("templateId") == RUN_PRE_SCRIPT else "script",
                    "id": script_id, "name": None}
        elif script_id:
            runs = {"kind": "pre-script" if step.get("templateId") == RUN_PRE_SCRIPT
                    or any(isinstance(p, dict) and p.get("key") in _SCRIPT_KEYS for p in step.get("parameters") or [])
                    else "script", "id": script_id, "name": None}
            nested = {}
            children = await _fetch(budget, depth, seen, script_id, runs, entry,
                                    lambda: clients.test_mgmt.get_test_script(script_id, branch), keep=nested)
            child_website = nested.get("websiteId") or website_id
        elif _is_user_test_step(step):
            function_id = step["templateId"]
            runs = {"kind": "user test step", "id": function_id, "name": None}
            function = {}
            children = await _fetch(budget, depth, seen, function_id, runs, entry,
                                    lambda: clients.asset.get_common_function(function_id), keep=function)
            child_website = function.get("websiteId") or website_id
        if runs:
            entry["runs"] = runs
            if children:
                out.extend(await expand_steps(clients, children, branch, number,
                                              {k: runs[k] for k in ("kind", "id", "name")},
                                              depth + 1, seen | {runs.get("id")}, budget, child_website))
    return out


async def _fetch(budget, depth, seen, asset_id, runs, entry, load, keep: dict = None):
    if asset_id in seen:
        entry["note"] = "Runs itself again - not opened a second time."
        return None
    if depth >= MAX_DEPTH or budget.fetches >= MAX_FETCHES:
        entry["note"] = "Not opened: nested too deep for one read. Read it on its own."
        return None
    budget.fetches += 1
    try:
        asset = await load()
    except Exception as e:  # one unreadable pre-script must not hide the rest of the script
        entry["note"] = f"Could not read it: {e}"
        return None
    if not isinstance(asset, dict):
        return None
    runs["name"] = asset.get("name")
    if keep is not None:
        keep.update(asset)
    return asset.get("testSteps") or []


def find_step(expanded: list, number: str):
    """The entry with this run number ("4", "1.4", "3.2.1"), or None."""
    number = str(number).strip()
    return next((entry for entry in expanded if entry["number"] == number), None)


def steps_inside(expanded: list, call_number: str, up_to: str = None) -> list:
    """The steps a call ran, in order - up to and including `up_to` when given."""
    prefix = f"{call_number}."
    found = []
    for entry in expanded:
        if entry["number"].startswith(prefix):
            found.append({k: entry[k] for k in ("number", "title", "element", "skipped") if k in entry})
            if up_to and entry["number"] == up_to:
                break
    return found
