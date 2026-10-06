"""
Does every step's template run on the script's platform?

Step templates are built for one platform - WEB, MOBILE or DESKTOP - and a step from another one
runs that platform's code. A mobile "Verify {{text}} is present on screen" went into a web script:
its title read like the right check, and it executes a mobile-only page-source read that gives a
web page no time to load. Nothing between choosing the template and running the script said so.

So search results can be narrowed to the script's platform. A check with a direct counterpart on
the script's platform - same parameters, same meaning - is switched to it before writing; any other
step from another platform is refused, naming it. Neutral templates - API calls, conditions, loops,
a user's own functions - carry another type or none and fit any script.

Two more corrections ride on the same lookups. A step runs by its templateId while the platform
stores whatever title it is sent, so an id paired with another template's title ("Click
{{ui-locator}}" on the id of "Deselect option by index") is moved to the template that has that
title. And a password typed with the plain "Enter" step is moved to the encrypted one, which takes
the same parameters and stores the value encrypted.

Lookups that fail are not reported: a check that cannot run must never become the failure.
"""
import asyncio

from src.tools import password_steps as _pwsteps

PLATFORMS = ("WEB", "MOBILE", "DESKTOP")

# Mobile check -> its web counterpart; each pair takes the same parameter keys.
_MOBILE_TO_WEB = {
    "template-id-161": "template-id-74",  # Verify {{text}} is present on screen -> ... on the page
    "template-id-159": "template-id-82",  # Verify {{ui-locator}} is displayed -> Verify visibility of
    "template-id-160": "template-id-99",  # Verify {{ui-locator}} is not displayed -> ... is not visible
}
_COUNTERPARTS = {"WEB": _MOBILE_TO_WEB, "MOBILE": {web: mobile for mobile, web in _MOBILE_TO_WEB.items()}}
_MAX_CONCURRENT_LOOKUPS = 8
_PLAIN_ENTER, _ENCRYPTED_ENTER = "template-id-3", "template-id-98"


def platform_of(script_type) -> str | None:
    """The platform a script runs on; WEB when unset, None for a type this check does not cover."""
    value = str(script_type or "WEB").strip().upper()
    return value if value in PLATFORMS else None


def runs_on(template_type, platform: str | None) -> bool:
    if platform is None:
        return True
    value = str(template_type or "").strip().upper()
    return value not in PLATFORMS or value == platform


def filter_templates(templates, platform: str | None):
    if not isinstance(templates, list) or platform is None:
        return templates
    return [t for t in templates if not isinstance(t, dict) or runs_on(t.get("type"), platform)]


def _template_refs(steps):
    refs = []

    def walk(step_list):
        for index, step in enumerate(step_list or []):
            if not isinstance(step, dict):
                continue
            template_id = step.get("templateId")
            # Built-ins only: a user's own function has a UUID id and no platform of its own.
            if isinstance(template_id, str) and template_id.startswith("template-id-"):
                refs.append((step.get("sequence") or index + 1, template_id, step))
            walk(step.get("subTestSteps"))

    walk(steps)
    return refs


async def _by_title(clients, title: str) -> list:
    try:
        found = await clients.test_mgmt.search_templates(title)
    except Exception:
        return []
    return [t for t in found or [] if isinstance(t, dict) and str(t.get("templateId", "")).startswith("template-id-")
            and (t.get("templateTitle") or "").strip() == title]


def _use(step: dict, template_id: str, title: str):
    step["templateId"] = template_id
    step["templateTitle"] = title
    step.pop("testStepTitle", None)  # rebuilt by the platform from template + parameters


async def mismatches(clients, steps, platform: str | None, resolution: dict = None) -> list:
    """
    Corrects steps in place - a templateId paired with another template's title, another platform's
    template with a counterpart, a password typed with the plain Enter step - and returns one line
    per step it could not correct.
    """
    refs = _template_refs(steps)
    if not refs:
        return []
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_LOOKUPS)

    async def lookup(template_id):
        async with semaphore:
            try:
                return template_id, await clients.test_mgmt.get_template(template_id)
            except Exception:
                return template_id, None

    counterparts = _COUNTERPARTS.get(platform, {}) if platform else {}
    wanted = ({t for _, t, _ in refs} | {counterparts[t] for _, t, _ in refs if t in counterparts}
              | {_ENCRYPTED_ENTER})
    templates = dict(await asyncio.gather(*(lookup(t) for t in sorted(wanted))))
    secret_ids = {e["locator_id"] for e in (resolution or {}).get("resolved") or []
                  if _pwsteps._SECRET_FIELD.search(str(e.get("locator_name") or ""))}
    found = []
    for sequence, template_id, step in refs:
        template = templates.get(template_id)
        sent_title = (step.get("templateTitle") or "").strip()
        if isinstance(template, dict) and sent_title and sent_title != (template.get("templateTitle") or "").strip():
            titled = await _by_title(clients, sent_title)
            if len(titled) != 1:
                found.append(f'step {sequence} has templateId {template_id} ("{template.get("templateTitle")}") but the '
                             f'title "{sent_title}", which {"no template has" if not titled else "several templates have"}')
                continue
            template_id = titled[0]["templateId"]
            template = titled[0]
            _use(step, template_id, sent_title)
        if not isinstance(template, dict):
            continue
        encrypted = templates.get(_ENCRYPTED_ENTER)
        if (template_id == _PLAIN_ENTER and isinstance(encrypted, dict)
                and _pwsteps._targets_secret(step, secret_ids)):
            _use(step, _ENCRYPTED_ENTER, encrypted.get("templateTitle"))
            continue
        if runs_on(template.get("type"), platform):
            continue
        replacement = templates.get(counterparts.get(template_id))
        if isinstance(replacement, dict) and replacement.get("templateTitle"):
            step["templateId"] = counterparts[template_id]
            step["templateTitle"] = replacement["templateTitle"]
            step.pop("testStepTitle", None)  # rebuilt by the platform from template + parameters
            continue
        found.append(f'step {sequence} uses {template_id} "{template.get("templateTitle")}", '
                     f'a {str(template.get("type")).upper()} template')
    return found


def refusal(found: list, platform) -> dict:
    hint = (f' Search with search_step_templates(title, script_type="{platform}") for a {platform} script'
            f' (for text on a web page: template-id-74 "Verify {{{{text}}}} on the page").') if platform else ""
    return {"error": (
        "Nothing was written - " + "; ".join(found) + ". A step runs by its templateId, and a template "
        "from another platform runs that platform's code. Use each templateId and templateTitle exactly "
        "as search_step_templates returns them, as a pair." + hint
    )}
