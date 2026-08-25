import base64
import json
from dataclasses import dataclass
from typing import Mapping

# The gateway each shipped configuration profile points at. This mapping is what makes password
# sign-in environment-aware: an API token names its own gateway through its urlDetails.baseUrl
# claim, but a password carries no such claim, so naming a profile is the only way for
# testbots-login to know which environment to authenticate against. Without it, prod credentials
# get checked against whichever gateway happens to be configured — which reads as "wrong
# password" rather than "wrong environment".
PROFILE_BASE_URLS = {
    "dev": "https://api-dev.automationhq.ai",
    "staging": "https://api-staging.automationhq.ai",
    "prod": "https://api.automationhq.ai",
}

# The AHQ gateway hosts a token's own `urlDetails` claim is allowed to resolve to (plus whatever
# AHQ_MCP_EXTRA_BASE_URLS adds) — see base_url_from_claims below. Derived from the profile map so
# adding an environment cannot leave the allowlist behind and silently reject its own tokens.
KNOWN_BASE_URLS = tuple(PROFILE_BASE_URLS.values())


def decode_ahq_token(token: str) -> dict:
    """
    Decodes an AHQ ORGANIZATION JWT's claims WITHOUT verifying the signature — this is safe here
    because we're not authenticating the caller, just reading back identity fields the gateway
    itself already trusts (the gateway does the real signature/lookup validation against Mongo's
    `tokens` collection). Used to derive organizationId directly from the token instead of trusting
    a separately-configured value that can drift out of sync with it.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError):
        return {}


def base_url_from_claims(claims: dict, allowed_extra: frozenset[str] = frozenset()) -> str | None:
    """
    Every other AHQ client (standalone local agent, browser locator-spy extension, the
    frontend's own token controller) resolves its per-environment service URLs from the token's
    own `urlDetails` claim (a list of {"key": ..., "value": ...} pairs, e.g. key "baseUrl") —
    confirmed 2026-07-17 from a real prod ORGANIZATION token whose urlDetails.baseUrl was
    https://api.automationhq.ai. ahq-mcp-server was the one place still resolving AHQ API calls
    against a single fixed AHQ_BASE_URL setting instead, so a prod token pasted into a
    dev-hosted consent flow got validated against dev's gateway/DB and showed the wrong (or no)
    projects for that org.

    Restricted to an allowlist because decode_ahq_token does NOT verify the signature (see its
    own docstring) — trusting an arbitrary claim value here would let a crafted token turn this
    server into an open relay to any host. Returns None (caller falls back to its own configured
    default) if there's no "baseUrl" entry or its value isn't in the allowed set.
    """
    allowed = set(KNOWN_BASE_URLS) | allowed_extra
    for entry in claims.get("urlDetails") or []:
        if isinstance(entry, dict) and entry.get("key") == "baseUrl":
            value = str(entry.get("value") or "").rstrip("/")
            if value in allowed:
                return value
    return None


@dataclass(frozen=True)
class AhqCredentials:
    """
    Per-tenant AHQ credentials. Threaded through every client instead of read from a global
    singleton, so the same process can serve multiple orgs/users (hosted HTTP mode) while stdio
    mode keeps using one fixed set of credentials from .env.

    org_id is ALWAYS derived from the token's own `organizationId` claim, never from a
    separately-configured value — a stale/mismatched org_id silently writes real data into the
    wrong organization (confirmed live, 2026-07-08: a wrong AHQ_ORG_ID in .env matched neither the
    token's own org nor anything the user could see, and every API call still "succeeded" since the
    gateway doesn't validate the header against the token's claim). base_url is likewise resolved
    from the token's own `urlDetails.baseUrl` claim first (same as hosted mode's from_headers,
    below) — AHQ_BASE_URL in .env is now only a fallback for tokens that predate that claim or
    carry a base_url outside the allowlist. project_id has no equivalent claim in the token (an
    ORGANIZATION token isn't scoped to one project) and must still be configured explicitly.
    """

    base_url: str
    api_token: str
    org_id: str
    project_id: str
    # "api-key" sends the value as X-API-AUTH-KEY, which the gateway resolves by existence lookup
    # only — no user, no role. "bearer" sends it as Authorization: Bearer, the same path the web
    # app uses, where the gateway attaches the real user and their authorities. Password sign-in
    # on the consent page produces the latter, which is what makes a connector session carry the
    # signed-in user's own permissions instead of blanket org-wide access.
    auth_scheme: str = "api-key"

    @classmethod
    def from_settings(cls, settings) -> "AhqCredentials":
        claims = decode_ahq_token(settings.ahq_api_token)
        resolved_base_url = base_url_from_claims(claims, settings.extra_base_urls()) or settings.ahq_base_url
        return cls(
            base_url=resolved_base_url,
            api_token=settings.ahq_api_token,
            org_id=claims.get("organizationId", ""),
            project_id=settings.ahq_project_id,
        )

    @classmethod
    def from_headers(
        cls, headers: Mapping[str, str], base_url: str, allowed_extra_base_urls: frozenset[str] = frozenset()
    ) -> "AhqCredentials":
        # base_url is never taken from a caller-supplied HEADER — the only way to change it is
        # via the token's own urlDetails.baseUrl claim (base_url_from_claims), which is checked
        # against an explicit allowlist, not trusted verbatim.
        token = headers.get("X-API-AUTH-KEY", "")

        # A first-party caller that already holds the signed-in user's platform JWT (the AI Test
        # Builder in ahq-test-management-services is the first) forwards it rather than swapping
        # in a service credential. That matters twice over: an ORGANIZATION api-key is scoped to
        # whichever org minted it, so one configured key pins an entire multi-tenant deployment
        # to a single organization; and it carries no user, so the gateway attaches no roles and
        # every action is audited to the support user.
        #
        # org_id comes from the HEADER on this path, unlike the api-key path below. The gateway's
        # own JWT issuance embeds only sub/userId/username/roles — there is no organizationId
        # claim to read — so the header is the only signal available. It is no weaker than it
        # looks: the gateway has already validated the JWT's signature by the time the request
        # reaches us, and every downstream AHQ service authorizes the org against that user.
        if not token:
            authorization = headers.get("Authorization", "")
            if authorization:
                bearer = authorization[7:] if authorization[:7].lower() == "bearer " else authorization
                claims = decode_ahq_token(bearer)
                return cls(
                    base_url=base_url_from_claims(claims, allowed_extra_base_urls) or base_url,
                    api_token=bearer,
                    org_id=headers.get("org-id", ""),
                    project_id=headers.get("projectId", ""),
                    auth_scheme="bearer",
                )

        org_id = ""
        resolved_base_url = base_url
        if token:
            try:
                claims = decode_ahq_token(token)
            except Exception:
                claims = {}
            org_id = claims.get("organizationId", "")
            resolved_base_url = base_url_from_claims(claims, allowed_extra_base_urls) or base_url
        return cls(
            base_url=resolved_base_url,
            api_token=token,
            org_id=org_id,
            project_id=headers.get("projectId", ""),
        )
