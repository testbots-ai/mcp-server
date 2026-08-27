"""
Which account does this script sign in as?

The platform cannot answer that. A bot has no application identity of its own — it authenticates
to the app under test by *typing credentials from the script's own steps*, so the answer lives in
the step parameters and nowhere else. `BotExecution.createdBy` records who triggered the run,
which is a different question and routinely mistaken for this one.

Getting it wrong is expensive and quiet. A whole debugging session was spent "verifying live in
Chrome" while signed in as a Site Admin, when the failing scripts ran as a Tester whose navigation
menu does not contain the pages being verified. Every check looked correct and none of them tested
the thing that mattered — role-gated elements were invisible to the account the bot actually used.

What is reported, and what deliberately is not:

- Vault / configuration / variable / data-column / parameter references are reported **by name**.
  A name is what identifies the account and is safe to show.
- A literal that looks like an identity (contains `@`) is shown, because that IS the answer.
- Every other literal is reported as present-but-withheld. Passwords reach these steps as plain
  literals, and printing them would put a working credential in the transcript — the same leak
  just closed for grid access keys.
"""

# TypeValuePair type codes (ActionLibraryServices.typeAwareDisplay).
_SOURCE_BY_TYPE = {
    0: "literal",
    1: "data_column",
    2: "configuration",
    3: "variable",
    5: "parameter",
    6: "faker",
    7: "vault",
}

# Parameter keys and title words that mark a step as carrying sign-in input.
_CREDENTIAL_HINTS = (
    "password", "passwd", "pwd", "secret", "token", "apikey", "api-key",
    "username", "user-name", "email", "login", "credential",
)

_WITHHELD = "[present, not shown — a literal here may be a real password]"


def _looks_like_identity(value: str) -> bool:
    return "@" in value and " " not in value.strip()


def _is_credential_context(step: dict, key: str) -> bool:
    title = (step.get("testStepTitle") or step.get("templateTitle") or "").lower()
    hay = f"{key.lower()} {title}"
    return any(hint in hay for hint in _CREDENTIAL_HINTS)


def describe_credentials(script: dict) -> dict:
    """
    Summarise the credential sources a script's steps use.

    Returns {} when the script references none, so the caller can attach it unconditionally
    without adding noise to the majority of scripts that never sign in.
    """
    if not isinstance(script, dict):
        return {}

    found, seen = [], set()

    def walk(steps):
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            for param in step.get("parameters") or []:
                if not isinstance(param, dict):
                    continue
                key = param.get("key") or ""
                value = param.get("value")
                if not isinstance(value, dict) or "type" not in value:
                    continue
                if not _is_credential_context(step, key):
                    continue

                source = _SOURCE_BY_TYPE.get(value.get("type"), "unknown")
                raw = value.get("value")
                shown = None
                if source == "literal":
                    text = str(raw) if raw is not None else ""
                    # Encrypted-template literals already come back as asterisks; treat those as
                    # withheld too rather than reporting a row of stars as the account name.
                    if text and _looks_like_identity(text):
                        shown = text
                    elif text.strip("*"):
                        shown = _WITHHELD
                    else:
                        shown = _WITHHELD
                else:
                    # A reference is identified by its NAME, which is the useful part.
                    shown = str(raw) if raw is not None else None

                entry = {
                    "sequence": step.get("sequence"),
                    "step": step.get("testStepTitle") or step.get("templateTitle"),
                    "parameter": key,
                    "source": source,
                    "value": shown,
                }
                fingerprint = (entry["parameter"], entry["source"], entry["value"])
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                found.append(entry)
            walk(step.get("subTestSteps"))

    walk(script.get("testSteps"))
    if not found:
        return {}

    identities = [f["value"] for f in found
                  if f["source"] == "literal" and f["value"] and f["value"] != _WITHHELD]
    references = sorted({f["value"] for f in found if f["source"] != "literal" and f["value"]})

    out = {"sources": found}
    if identities:
        out["signs_in_as"] = identities
    if references:
        out["references"] = references
    out["why_this_matters"] = (
        "This is the account the SCRIPT authenticates as, which is not necessarily the account "
        "you are signed in as while checking the app by hand. Role-gated elements visible to you "
        "may be invisible to it — verify against this identity, not your own session."
    )
    return out
