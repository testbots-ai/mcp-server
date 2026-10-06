"""
Does every step's template run on the script's platform?

Step templates are built for one platform - WEB, MOBILE or DESKTOP - and a step from another one
runs that platform's code. A mobile "Verify {{text}} is present on screen" went into a web script:
its title read like the right check, and it executes a mobile-only page-source read that gives a
web page no time to load. Nothing between choosing the template and running the script said so.

So search results can be narrowed to the script's platform, and a write that uses another
platform's template is refused, naming the step. Neutral templates - API calls, conditions,
loops, a user's own functions - carry another type or none and fit any script.

Lookups that fail are not reported: a check that cannot run must never become the failure.
"""
import asyncio

PLATFORMS = ("WEB", "MOBILE", "DESKTOP")
_MAX_CONCURRENT_LOOKUPS = 8


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
                refs.append((step.get("sequence") or index + 1, template_id))
            walk(step.get("subTestSteps"))

    walk(steps)
    return refs


async def mismatches(clients, steps, platform: str | None) -> list:
    """One line per step whose template belongs to another platform."""
    if platform is None:
        return []
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

    templates = dict(await asyncio.gather(*(lookup(t) for t in sorted({t for _, t in refs}))))
    found = []
    for sequence, template_id in refs:
        template = templates.get(template_id)
        if not isinstance(template, dict) or runs_on(template.get("type"), platform):
            continue
        found.append(f'step {sequence} uses {template_id} "{template.get("templateTitle")}", '
                     f'a {str(template.get("type")).upper()} template')
    return found


def refusal(found: list, platform: str) -> dict:
    return {"error": (
        f"Nothing was written - this is a {platform} script, and " + "; ".join(found) + ". A template "
        f"from another platform runs that platform's code here. Search again with "
        f"search_step_templates(title, script_type=\"{platform}\") and use the {platform} equivalent "
        f"(for text on a web page: template-id-74 \"Verify {{{{text}}}} on the page\")."
    )}
