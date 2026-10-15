"""
Does every vault secret a step names actually exist?

A step that types a password from the vault stores only the secret's NAME ({"type": 7, "value":
"<name>"}). The platform does not check the name on save: a step naming a secret that does not
exist is accepted, shows an empty value in the script editor, and types nothing at run time - the
sign-in then fails as if the password were wrong. Found live: asked to replace a wrong typed
password, a model wrote {"type": 7, "value": "password"} - a plausible name, not a real one.

So a step write naming an unknown secret is refused, with the names that do exist, before
anything is saved. Fail-open: when the vault cannot be listed, the write goes ahead unchecked.
"""
from src.clients.test_mgmt_client import TYPE_VALUE_PAIR_CODES

VAULT = TYPE_VALUE_PAIR_CODES["vault"]
STEP_WRITE_TOOLS = ("create_test_script", "add_test_steps", "replace_test_step", "update_test_script",
                    "create_common_function", "update_common_function")


def vault_names(steps) -> list:
    """Every vault secret name the steps reference, raw or friendly form, in order."""
    found = []

    def walk(items):
        for step in items or []:
            if not isinstance(step, dict):
                continue
            for param in step.get("parameters") or []:
                value = param.get("value") if isinstance(param, dict) else None
                if not isinstance(value, dict):
                    continue
                name = value.get("vault") if len(value) == 1 else value.get("value") if value.get("type") == VAULT else None
                if isinstance(name, str) and name.strip() and name not in found:
                    found.append(name)
            walk(step.get("subTestSteps"))
    walk(steps)
    return found


async def unknown_secrets(clients, steps) -> tuple:
    """(names that do not exist, names that do); ([], []) when nothing is referenced or the vault cannot be read."""
    wanted = vault_names(steps)
    if not wanted:
        return [], []
    try:
        listed = await clients.config.list_config_vault_secrets()
    except Exception:
        return [], []
    items = listed if isinstance(listed, list) else (listed or {}).get("content") or []
    existing = [item.get("name") for item in items if isinstance(item, dict) and item.get("name")]
    return [name for name in wanted if name not in existing], existing


def refusal(missing: list, existing: list) -> dict:
    close = {name: [e for e in existing if name.lower() in e.lower() or e.lower() in name.lower()] for name in missing}
    hints = "; ".join(f'"{name}" - did you mean {", ".join(repr(c) for c in found)}?' for name, found in close.items() if found)
    return {"error": (
        f"Nothing was saved: no vault secret is named {', '.join(repr(n) for n in missing)}. A step that "
        "names a secret that does not exist types nothing at run time. Use one of the existing secrets "
        f"({', '.join(existing) if existing else 'the vault is empty'}), create the secret first with "
        "create_config_vault_secret, or type UPDATE_PASSWORD and tell the user to set it."
        + (f" {hints}" if hints else "")
    ), "existing_vault_secrets": existing}
