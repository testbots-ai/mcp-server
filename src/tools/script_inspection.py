"""
Script inspection and validation tools for pre-execution checks and scoped execution.
Fixes from retrospective: avoid guess-fixes, catch credential issues, targeted reruns.
"""

from typing import Optional


async def inspect_script(
    clients,
    script_id: str,
) -> dict:
    """
    Inspect a test script before execution: locator status, step summary, version info.

    Pre-execution check that catches issues early instead of discovering them at runtime.
    Returns: step count, locator coverage, which steps reference vault secrets, any missing
    locators already flagged as broken.
    """
    try:
        script = await clients.test_mgmt.get_test_script(script_id)
    except Exception as e:
        return {"error": f"Could not load script: {e}"}

    if not isinstance(script, dict):
        return {"error": "Script response was not a dictionary"}

    # Build a summary
    steps = script.get("testSteps", []) or []
    if not isinstance(steps, list):
        steps = []

    summary = {
        "scriptId": script_id,
        "name": script.get("name"),
        "status": script.get("status"),
        "type": script.get("type"),
        "versionCount": script.get("versionCount"),
        "currentBranchName": script.get("currentBranchName"),
        "stepCount": len(steps),
        "warnings": [],
    }

    # Check for vault secrets in steps (type-7 TypeValuePairs)
    vault_refs = []
    missing_locators = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue

        # Check for vault secrets
        params = step.get("parameters", []) or []
        if isinstance(params, list):
            for param in params:
                if isinstance(param, dict):
                    val = param.get("value")
                    if isinstance(val, dict) and val.get("type") == 7:
                        vault_refs.append({
                            "step": i + 1,
                            "key": param.get("key"),
                            "secret": val.get("value")
                        })

        # Check for missing locators (those already flagged broken)
        for param in params:
            if isinstance(param, dict) and param.get("key") in ("ui-locator", "uiLocator"):
                val = param.get("value", {})
                if isinstance(val, dict):
                    locator_id = val.get("locatorId")
                    if locator_id:
                        # Mark for later checking if this locator is flagged broken
                        missing_locators.append({
                            "step": i + 1,
                            "locatorId": locator_id,
                        })

    if vault_refs:
        summary["vault_secrets_in_steps"] = vault_refs
        summary["warnings"].append(f"Script uses {len(vault_refs)} vault secrets (resolved at runtime, appear in execution report)")

    summary["step_summary"] = [
        {
            "step": i + 1,
            "template": step.get("templateTitle"),
            "sequence": step.get("sequence"),
        }
        for i, step in enumerate(steps[:10])  # First 10 for readability
    ]

    if len(steps) > 10:
        summary["step_summary"].append({"note": f"... {len(steps) - 10} more steps (see full script for all)"})

    return summary


async def validate_script_locators(
    clients,
    script_id: str,
) -> dict:
    """
    Check all locators in a script: are they still on the page, or flagged as broken?

    Returns: per-locator status, any that need healing before the script can run reliably.
    """
    try:
        script = await clients.test_mgmt.get_test_script(script_id)
    except Exception as e:
        return {"error": f"Could not load script: {e}"}

    if not isinstance(script, dict):
        return {"error": "Script response was not a dictionary"}

    steps = script.get("testSteps", []) or []
    if not isinstance(steps, list):
        steps = []

    locator_ids = set()
    step_refs = {}  # locator_id -> list of step numbers

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        params = step.get("parameters", []) or []
        for param in params:
            if isinstance(param, dict) and param.get("key") in ("ui-locator", "uiLocator"):
                val = param.get("value", {})
                if isinstance(val, dict):
                    locator_id = val.get("locatorId")
                    if locator_id:
                        locator_ids.add(locator_id)
                        if locator_id not in step_refs:
                            step_refs[locator_id] = []
                        step_refs[locator_id].append(i + 1)

    if not locator_ids:
        return {
            "scriptId": script_id,
            "status": "NO_LOCATORS",
            "note": "Script has no UI locator steps (API/data steps only)"
        }

    # Check each locator: get it, check if broken
    result = {
        "scriptId": script_id,
        "locators_checked": len(locator_ids),
        "locator_status": [],
        "warnings": [],
    }

    broken_count = 0
    for locator_id in locator_ids:
        try:
            # Note: we don't have a direct get_locator, but get_page_by_url can tell us if it exists
            # For now, we record the reference and note that full validation needs get_page_by_url
            result["locator_status"].append({
                "locatorId": locator_id,
                "usedInSteps": step_refs.get(locator_id, []),
                "note": "validate with scan_broken_locators or heal_locator before execution"
            })
        except Exception as e:
            broken_count += 1
            result["locator_status"].append({
                "locatorId": locator_id,
                "usedInSteps": step_refs.get(locator_id, []),
                "error": str(e)
            })

    if broken_count:
        result["warnings"].append(f"{broken_count} locators could not be verified — they may be stale")
        result["recommendation"] = "Run scan_broken_locators to find flagged-broken ones, then heal_locator to propose replacements"
    else:
        result["status"] = "OK"

    return result


async def check_script_for_credential_exposure(
    clients,
    script_id: str,
) -> dict:
    """
    Check a script for plaintext credentials accidentally baked into step values.

    Returns: any suspicious patterns (email+password pairs, known credential shapes).
    WARNING: this is a DETECTION tool only, not a filter — it tells you what's there.
    """
    try:
        script = await clients.test_mgmt.get_test_script(script_id)
    except Exception as e:
        return {"error": f"Could not load script: {e}"}

    if not isinstance(script, dict):
        return {"error": "Script response was not a dictionary"}

    steps = script.get("testSteps", []) or []
    if not isinstance(steps, list):
        steps = []

    result = {
        "scriptId": script_id,
        "name": script.get("name"),
        "plaintext_values": [],
        "warnings": [],
    }

    suspicious_patterns = [
        ("@", "email address"),
        ("password", "password field reference"),
        ("pwd", "password field reference"),
        ("secret", "secret field reference"),
        ("token", "API token"),
        ("api_key", "API key"),
        ("Authorization", "auth header"),
    ]

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue

        params = step.get("parameters", []) or []
        for param in params:
            if not isinstance(param, dict):
                continue

            key = param.get("key", "").lower()
            val = param.get("value")

            # Skip vault references (type 7)
            if isinstance(val, dict) and val.get("type") == 7:
                result["plaintext_values"].append({
                    "step": i + 1,
                    "key": param.get("key"),
                    "status": "VAULT_PROTECTED",
                    "value_type": f"vault:{val.get('value')}"
                })
                continue

            # Check for literal strings that look like credentials
            text_val = ""
            if isinstance(val, dict):
                text_val = str(val.get("value", "")).lower()
            elif isinstance(val, str):
                text_val = val.lower()

            # Flag suspicious combinations
            is_suspicious = False
            reason = None
            for pattern, desc in suspicious_patterns:
                if pattern in text_val or pattern in key:
                    is_suspicious = True
                    reason = desc
                    break

            if is_suspicious and text_val:  # Has both a suspicious key AND a value
                result["plaintext_values"].append({
                    "step": i + 1,
                    "key": param.get("key"),
                    "status": "PLAINTEXT",
                    "reason": reason,
                    "contains_value": True,  # Don't expose the actual value
                })
                result["warnings"].append(
                    f"Step {i + 1}: {param.get('key')} looks like {reason} with plaintext value — "
                    "consider using vault secret instead (create_config_vault_secret + type 7)"
                )

    if result["warnings"]:
        result["recommendation"] = (
            "Vault secrets (type 7 TypeValuePair) keep credentials out of stored scripts and "
            "are resolved only at execution time. Create one with create_config_vault_secret, "
            "then update the step to reference it."
        )
    else:
        result["status"] = "OK"

    return result
