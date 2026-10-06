"""
Does every step that types into a password field have a password to type?

A script is written from a description like "sign in with the password", and when no password was
given the model fills the gap with something that only looks finished: a run-time variable no
step ever stores, the text "the password you gave", a row of dots. The script saves cleanly and
fails at its login step on the first run - after the user has already moved on.

Such a script is still written: the user may rather not put a password in a chat, and the rest of
the script is good. But the result says plainly which step has no password, so the caller tells
the user to update it (or point it at a vault secret) before running.

Accepted as a password: a typed value that is not a stand-in, or a reference the platform resolves
- vault secret, global parameter, data column, parameter. A variable is accepted only when an
earlier step of the same write mentions it, i.e. a step that stores it.
"""
import json
import re

from src.clients.test_mgmt_client import TYPE_VALUE_PAIR_CODES

PLACEHOLDER_PASSWORD = "UPDATE_PASSWORD"

_SOURCE_BY_TYPE = {code: name for name, code in TYPE_VALUE_PAIR_CODES.items()}
_LOCATOR_KEYS = ("ui-locator", "uiLocator")
_SECRET_FIELD = re.compile(r"(?i)pass(word|code)?|pwd|secret|\bpin\b|otp")
_STAND_IN = re.compile(
    r"(?i)^(|password|pass|pwd|secret|update_password|change_?me|your password.*"
    r"|.*password you (gave|provided|shared|entered).*"
    r"|<[^>]*>|\{\{[^}]*}}|\$\{[^}]*}|[•*x.\-_ ]+|redacted|\*{3}redacted\*{3}|n/?a|todo|tbd)$")


def is_stand_in(text) -> bool:
    """Whether a typed value is a stand-in rather than a password."""
    return isinstance(text, str) and bool(_STAND_IN.match(text.strip()))


def _source_and_value(value):
    if isinstance(value, str):
        return "literal", value
    if not isinstance(value, dict):
        return None, None
    if len(value) == 1 and next(iter(value)) in TYPE_VALUE_PAIR_CODES:
        key = next(iter(value))
        return key, value[key]
    if "type" in value:
        return _SOURCE_BY_TYPE.get(value.get("type"), "unknown"), value.get("value")
    return None, None


def _title(step: dict) -> str:
    title = step.get("testStepTitle") or step.get("templateTitle") or ""
    return re.sub(r"\{\{[^}]*}}", "", str(title))


def _targets_secret(step: dict, secret_locator_ids: set) -> bool:
    if _SECRET_FIELD.search(_title(step)):
        return True
    for param in step.get("parameters") or []:
        if not isinstance(param, dict) or param.get("key") not in _LOCATOR_KEYS:
            continue
        value = param.get("value")
        if isinstance(value, dict):
            if value.get("locatorId") in secret_locator_ids:
                return True
            if _SECRET_FIELD.search(str(value.get("locatorName") or "")):
                return True
    return False


def problems(steps, resolution: dict = None, check_variables: bool = True) -> list:
    """
    One line per step that types into a password field without a real password.

    `check_variables` is off for edits to an existing script: a variable may be stored by a step
    already in the script, which an edit call does not carry.
    """
    secret_locator_ids = {
        entry["locator_id"] for entry in (resolution or {}).get("resolved") or []
        if _SECRET_FIELD.search(str(entry.get("locator_name") or ""))
    }
    found = []

    def walk(step_list):
        step_list = [s for s in step_list or [] if isinstance(s, dict)]
        for index, step in enumerate(step_list):
            if _targets_secret(step, secret_locator_ids):
                label = step.get("sequence") or index + 1
                for param in step.get("parameters") or []:
                    if not isinstance(param, dict) or param.get("key") in _LOCATOR_KEYS:
                        continue
                    source, value = _source_and_value(param.get("value"))
                    if source == "literal" and is_stand_in(value):
                        shown = value if value else "nothing"
                        found.append(f'step {label} types "{str(shown)[:30]}", which is not a password')
                    elif source == "variable" and check_variables and not _stored_earlier(str(value or ""), step_list[:index]):
                        found.append(f'step {label} uses the variable "{value}", which no earlier step stores')
            walk(step.get("subTestSteps"))

    walk(steps)
    return found


def _stored_earlier(variable: str, earlier: list) -> bool:
    if not variable.strip():
        return False
    needle = json.dumps(variable)
    return any(needle in json.dumps(step) for step in earlier)


def annotate(result, steps, resolution: dict = None, check_variables: bool = True):
    """Attach a password warning to a successful write that has a password step without one."""
    if not isinstance(result, dict) or "error" in result:
        return result
    found = problems(steps, resolution, check_variables)
    if found:
        result["password_warning"] = (
            "Password not set - " + "; ".join(found) + ". The script was saved, but its login will "
            "fail until the password step is updated. Tell the user plainly to update it - with the "
            "real password or a vault secret ({\"vault\": \"<secret name>\"}) - before running it."
        )
    return result
