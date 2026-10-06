"""
Does every check that follows a click wait for the page first?

A "Verify ..." step is one immediate read with no retry. Straight after a click that navigates or
re-renders, it reads the page the click was meant to leave and fails - on a script that looks
right. The rule to add a wait is in every generation guide and was still skipped: a generated
navigation script went Click -> Verify -> Click -> Verify with no wait at all, and failed one step
at a time across three runs.

So a write that has a check directly after a click comes back with a warning naming the steps.
Only steps within the same call are compared; the step before an inserted one is not visible here.
"""
import re

_PLACEHOLDER = re.compile(r"\{\{[^}]*}}")


def _kind(step) -> str:
    if not isinstance(step, dict):
        return ""
    title = _PLACEHOLDER.sub("", str(step.get("templateTitle") or step.get("testStepTitle") or "")).strip().lower()
    if title.startswith("click") or title.startswith("double click") or title.startswith("submit"):
        return "click"
    if title.startswith("verify"):
        return "check"
    return ""


def problems(steps) -> list:
    """One line per check that runs straight after a click."""
    found = []

    def walk(step_list):
        step_list = [s for s in step_list or [] if isinstance(s, dict)]
        for index in range(1, len(step_list)):
            if _kind(step_list[index - 1]) == "click" and _kind(step_list[index]) == "check":
                before = step_list[index - 1].get("sequence") or index
                after = step_list[index].get("sequence") or index + 1
                found.append(f"step {after} checks the page straight after the click in step {before}")
        for step in step_list:
            walk(step.get("subTestSteps"))

    walk(steps)
    return found


def annotate(result, steps):
    """Attach a wait warning to a successful write that checks straight after a click."""
    if not isinstance(result, dict) or "error" in result:
        return result
    found = problems(steps)
    if found:
        result["wait_warning"] = (
            "; ".join(found) + ". A check is a single immediate read, so it sees the page the click "
            "was leaving. Insert a wait between each pair - \"Wait for visibility of {{ui-locator}} for "
            "{{number}} seconds\" on an element of the next page, or \"Wait for {{number}} seconds\" - "
            "with replace_test_step or add_test_steps, before running the script.")
    return result
