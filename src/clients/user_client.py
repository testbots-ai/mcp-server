from src.clients.base_client import BaseAhqClient
from src.config.ahq_services import USER_MGMT_SVC

# Archive Manager entity -> route prefix. All *ArchiveController classes live in
# ahq-user-management-services (confirmed against the real controllers, not just the frontend's
# folder naming) and extend one generic ArchiveController<T, ID> with a uniform contract:
#   GET  {prefix}/archived?page=&size=&search=   (organizationId+projectId headers)
#   POST {prefix}/{id}/archive                    (body: {"archiveReason": ...}, optional)
#   POST {prefix}/{id}/restore
#   DELETE {prefix}/{id}/permanent
# EXCEPTION — "locator": LocatorArchiveController does NOT extend the base class; its list is a
# GET on the prefix root (no /archived suffix) and permanent delete is DELETE /{id} (no
# /permanent suffix). There is no locator archive action at all (locators land in the archive as
# a side-effect of page/website deletion elsewhere).
# NOT HERE — "recorded_script": its archive endpoints live on RecordedScriptController in
# ahq-test-management-services; the dispatcher routes that entity to TestMgmtClient instead.
ARCHIVE_ENTITY_PATHS = {
    "epic": "/api/epics",
    "story": "/api/stories",
    "website": "/api/websites",
    "page": "/api/pages",
    "locator": "/api/archived-locators",
    "test_script": "/api/test-scripts",
    "test_suite": "/api/test-suites",
    "test_bot": "/api/test-bots",
    "test_bot_folder": "/api/testbot-folders",
}


class UserClient(BaseAhqClient):
    def __init__(self, credentials=None, http_client=None):
        super().__init__(USER_MGMT_SVC, credentials, http_client)

    async def get_current_user(self) -> dict:
        # NOTE: 500s ("No value present") for ORGANIZATION tokens with no userId claim — a
        # server-side quirk, not a client bug. _get_context falls back to token claims.
        return await self.get("/rest/api/users/me")

    async def list_projects(self) -> list:
        # GET /rest/api/projects (bare) has NO handler on ProjectController — every list mapping
        # is org-scoped. The old path 405'd since day one; the real one is
        # /organizations/{orgId}/all.
        result = await self.get(
            f"/rest/api/projects/organizations/{self._credentials.org_id}/all")
        return result if isinstance(result, list) else result.get("content", result)

    async def registration_info(self, email: str) -> dict:
        """Who this email belongs to: userId, organizationId, projectId, name, userRole.

        This is what the web app calls immediately after sign-in (Checking.tsx -> callRegistrations
        InfoApi with the JWT's own `sub`), and it is the only lookup that works before an
        organization is known — it takes no org-id header, which is precisely the chicken-and-egg
        the consent flow has.

        Verified against a real login HAR 2026-08-03. `/users/me` was the obvious-looking choice
        and is the wrong one: it 500s "No value present" for a password JWT, because SecurityUtil
        resolves the caller from a `username` or `email` claim and LoginController's token carries
        only `sub`. Nothing in the product calls it that way.
        """
        return await self.post("/rest/api/registrations/info", json={"value": email}) or {}

    async def list_projects_for_user(self, user_id: str) -> list:
        """Projects this user personally holds a role in, as {id, name, org_id}.

        Field names verified against live dev 2026-08-03: the documents use `_id` / `projectName` /
        `orgId` — note `orgId`, NOT the `organizationId` every other entity uses, which is exactly
        the sort of thing that silently produces a list of Nones.

        Sourced from UserRole records rather than the caller's own organization, so it reflects
        what the signed-in person can actually reach. Disabled projects are dropped: offering one
        leads to a selection that fails on first use.
        """
        result = await self.get(f"/rest/api/projects/users/{user_id}")
        raw = result if isinstance(result, list) else result.get("content") or result.get("projects") or []
        return [
            {"id": p.get("_id"), "name": p.get("projectName"), "org_id": p.get("orgId")}
            for p in raw
            if isinstance(p, dict) and p.get("_id") and p.get("isEnabled", True)
        ]

    async def list_users(self) -> list:
        result = await self.get("/rest/api/users")
        return result if isinstance(result, list) else result.get("content", result)

    # --- Archive Manager ---
    # The generic ArchiveController reads @RequestHeader("organizationId") — the third controller
    # family found using that spelling instead of the default "org-id" (after RecordedScript and
    # the config-services vault).
    def _archive_headers(self) -> dict:
        return {"organizationId": self._credentials.org_id}

    async def list_archived(self, entity_type: str, search: str = None, page: int = 0, size: int = 50) -> dict:
        base = ARCHIVE_ENTITY_PATHS[entity_type]
        path = base if entity_type == "locator" else f"{base}/archived"
        params = {"page": page, "size": size}
        if search:
            params["search"] = search
        return await self.get(path, params=params, extra_headers=self._archive_headers())

    async def restore_archived(self, entity_type: str, asset_id: str) -> dict:
        base = ARCHIVE_ENTITY_PATHS[entity_type]
        return await self.post(f"{base}/{asset_id}/restore", extra_headers=self._archive_headers())

    async def permanently_delete_archived(self, entity_type: str, asset_id: str) -> dict:
        base = ARCHIVE_ENTITY_PATHS[entity_type]
        suffix = "" if entity_type == "locator" else "/permanent"
        return await self.delete(f"{base}/{asset_id}{suffix}", extra_headers=self._archive_headers())

    async def create_org_token(self, org_id: str, user_id: str, base_url: str,
                               label: str = "", expiry_minutes: int = 525600) -> dict:
        """Mint a long-lived ORGANIZATION API token, returning the server's full response.

        Called with a sign-in JWT as the credential, so a person can obtain a token without
        opening the web UI. Three things about this endpoint are easy to get wrong:

        - It reads `organizationid` and `userid` — all lowercase, unlike the `org-id` every
          other controller in the platform uses.
        - `urlDetails` is mandatory: TokenService.validateUrlDetails rejects an empty list with
          "At least one microservice URL must be provided". Supplying `baseUrl` is also what
          makes the minted token name its own environment, which is what lets every later run
          resolve the gateway from the token instead of a configured setting.
        - There is a per-organization token limit, and hitting it is a 400, not a queue. The
          response's `remainingTokens` is the only warning a caller gets, so mint once and
          store the result — never per run.

        `expiry_minutes` defaults to the server's own default of 525600 (one year).
        """
        return await self.post(
            "/rest/api/tokens/generate/org",
            json={"username": label, "urlDetails": [{"key": "baseUrl", "value": base_url}]},
            extra_headers={
                "organizationid": org_id,
                "userid": user_id,
                "expiryMinutes": str(expiry_minutes),
            },
        )

    async def user_token_status(self, user_id: str) -> dict:
        """The signed-in person's own personal-token count: `activeTokenCount` and `remainingTokens`.

        Counted by owner only - other people's tokens in the organization never use these slots.
        """
        return await self.get("/rest/api/tokens/generate/user/tokenview", extra_headers={"userId": user_id})

    async def create_user_token(self, user_id: str, base_url: str,
                                label: str = "", expiry_minutes: int = 525600) -> dict:
        """Mint a long-lived personal (USER) API token for the signed-in person.

        The fallback when the organization is at its token limit: the per-user limit is separate,
        so a person can still connect when the organization's slots are all taken. The gateway
        accepts it exactly like an organization token (it only checks the value exists), and its
        claims carry the same `organizationId` and `urlDetails` the MCP server reads - plus the
        person's own `userId`, so work it does is attributed to them rather than to the org.
        Same `urlDetails` rule and one-year default expiry as create_org_token.
        """
        return await self.post(
            "/rest/api/tokens/generate/user",
            json={"username": label, "urlDetails": [{"key": "baseUrl", "value": base_url}]},
            extra_headers={
                "userId": user_id,
                "expiryMinutes": str(expiry_minutes),
            },
        )
