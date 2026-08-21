import base64
import json

from src.config.credentials import AhqCredentials, base_url_from_claims, decode_ahq_token


def _fake_jwt(**claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJub25lIn0.{payload}.fake-signature"


def _url_details(base_url: str) -> list[dict]:
    return [{"key": "baseUrl", "value": base_url}]


def test_decode_ahq_token_reads_claims():
    token = _fake_jwt(organizationId="org-1", organizationName="Acme")

    claims = decode_ahq_token(token)

    assert claims == {"organizationId": "org-1", "organizationName": "Acme"}


class _FakeSettings:
    ahq_base_url = "https://api-dev.automationhq.ai"
    ahq_api_token = ""
    ahq_project_id = "proj-1"

    def extra_base_urls(self):
        return frozenset()


def test_from_settings_derives_org_id_from_token_not_a_separate_setting():
    class FakeSettings(_FakeSettings):
        ahq_api_token = _fake_jwt(organizationId="org-from-token")

    creds = AhqCredentials.from_settings(FakeSettings())

    assert creds == AhqCredentials(
        base_url="https://api-dev.automationhq.ai",
        api_token=FakeSettings.ahq_api_token,
        org_id="org-from-token",
        project_id="proj-1",
    )


def test_from_settings_resolves_base_url_from_token_urldetails():
    # Stdio/.env mode must resolve base_url from the token the same way hosted mode's
    # from_headers already did — AHQ_BASE_URL in .env is only a fallback now, not the source
    # of truth, so a prod token used with a dev-configured .env still hits prod's own gateway.
    class FakeSettings(_FakeSettings):
        ahq_base_url = "https://api-dev.automationhq.ai"
        ahq_api_token = _fake_jwt(
            organizationId="org-from-token", urlDetails=_url_details("https://api.automationhq.ai")
        )

    creds = AhqCredentials.from_settings(FakeSettings())

    assert creds.base_url == "https://api.automationhq.ai"


def test_from_settings_falls_back_to_ahq_base_url_when_token_has_no_urldetails():
    class FakeSettings(_FakeSettings):
        ahq_api_token = _fake_jwt(organizationId="org-from-token")

    creds = AhqCredentials.from_settings(FakeSettings())

    assert creds.base_url == "https://api-dev.automationhq.ai"


def test_from_headers_derives_org_id_from_token_ignoring_org_id_header():
    # A caller-sent "org-id" header must NOT be trusted for org_id — only the token's own
    # organizationId claim. This is exactly the class of bug that motivated this design: a
    # stale/wrong org-id can silently write real data into the wrong organization since the
    # gateway itself doesn't validate the header against the token's claim.
    token = _fake_jwt(organizationId="org-from-token")
    headers = {"X-API-AUTH-KEY": token, "org-id": "org-that-should-be-ignored", "projectId": "caller-project"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.api_token == token
    assert creds.org_id == "org-from-token"
    assert creds.project_id == "caller-project"


def test_from_headers_base_url_is_never_taken_from_caller():
    # A caller could send an X-Forwarded-Host or similar, but from_headers only accepts
    # base_url as an explicit keyword — there's no header key that can override it.
    headers = {"X-API-AUTH-KEY": "t", "org-id": "o", "projectId": "p", "base_url": "https://evil.example.com"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.base_url == "https://api-dev.automationhq.ai"


def test_from_headers_missing_headers_default_to_empty_string():
    creds = AhqCredentials.from_headers({}, base_url="https://api-dev.automationhq.ai")

    assert creds.api_token == ""
    assert creds.org_id == ""
    assert creds.project_id == ""


def test_base_url_from_claims_resolves_known_prod_host():
    # A real prod ORGANIZATION token's urlDetails.baseUrl (confirmed live 2026-07-17) — every
    # other AHQ client (standalone agent, browser extension, frontend) already reads this same
    # claim to find its own environment's gateway; ahq-mcp-server previously ignored it and
    # always used its own fixed AHQ_BASE_URL, which broke prod tokens on the dev-hosted server.
    claims = {"urlDetails": _url_details("https://api.automationhq.ai")}
    assert base_url_from_claims(claims) == "https://api.automationhq.ai"


def test_base_url_from_claims_rejects_url_outside_allowlist():
    # decode_ahq_token does not verify the signature, so an arbitrary urlDetails.baseUrl value
    # must NOT be trusted verbatim — that would let a crafted token turn this server into an
    # open relay to any host.
    claims = {"urlDetails": _url_details("https://evil.example.com")}
    assert base_url_from_claims(claims) is None


def test_base_url_from_claims_honors_extra_allowlist():
    # A self-hosted gateway, deliberately not one of PROFILE_BASE_URLS - the point of the extra
    # allowlist is hosts this build has never heard of.
    claims = {"urlDetails": _url_details("https://ahq.example-partner.com")}
    assert base_url_from_claims(claims) is None
    assert base_url_from_claims(claims, frozenset({"https://ahq.example-partner.com"})) == (
        "https://ahq.example-partner.com"
    )


def test_base_url_from_claims_missing_urldetails_returns_none():
    assert base_url_from_claims({"organizationId": "org-1"}) is None


def test_from_headers_resolves_base_url_from_prod_token_urldetails():
    # The actual bug this fixes: a prod org token pasted through the dev-hosted consent/legacy
    # header path must resolve prod's own gateway, not this server's fixed AHQ_BASE_URL.
    token = _fake_jwt(organizationId="org-1", urlDetails=_url_details("https://api.automationhq.ai"))
    headers = {"X-API-AUTH-KEY": token, "projectId": "proj-1"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.base_url == "https://api.automationhq.ai"


def test_from_headers_falls_back_to_default_when_token_has_no_urldetails():
    token = _fake_jwt(organizationId="org-1")
    headers = {"X-API-AUTH-KEY": token, "projectId": "proj-1"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.base_url == "https://api-dev.automationhq.ai"


def test_from_headers_accepts_a_user_bearer_jwt():
    # A first-party caller forwards the signed-in user's own platform JWT instead of a service
    # api-key, so the gateway attaches that user and their roles rather than blanket org-wide
    # access. org_id comes from the header here because the gateway's JWT carries no
    # organizationId claim to read.
    token = _fake_jwt(sub="user-1")
    headers = {"Authorization": f"Bearer {token}", "org-id": "org-from-header", "projectId": "p-1"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.auth_scheme == "bearer"
    assert creds.api_token == token  # the "Bearer " prefix is stripped; the client re-adds it
    assert creds.org_id == "org-from-header"
    assert creds.project_id == "p-1"


def test_from_headers_bearer_without_prefix_is_accepted():
    # AHQ's own frontend sends the raw token with no "Bearer " prefix, and services forward the
    # header verbatim, so both shapes reach us.
    token = _fake_jwt(sub="user-1")
    headers = {"Authorization": token, "org-id": "org-1", "projectId": "p-1"}

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.api_token == token
    assert creds.auth_scheme == "bearer"


def test_api_key_still_wins_when_both_headers_are_present():
    # Existing header clients must be unaffected by the bearer path being added.
    api_key = _fake_jwt(organizationId="org-from-token")
    headers = {
        "X-API-AUTH-KEY": api_key,
        "Authorization": f"Bearer {_fake_jwt(sub='someone-else')}",
        "org-id": "ignored",
        "projectId": "p-1",
    }

    creds = AhqCredentials.from_headers(headers, base_url="https://api-dev.automationhq.ai")

    assert creds.auth_scheme == "api-key"
    assert creds.api_token == api_key
    assert creds.org_id == "org-from-token"
