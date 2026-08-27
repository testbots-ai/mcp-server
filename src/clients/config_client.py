from src.clients.base_client import BaseAhqClient
from src.config.ahq_services import CONFIG_SVC


class ConfigClient(BaseAhqClient):
    def __init__(self, credentials=None, http_client=None):
        super().__init__(CONFIG_SVC, credentials, http_client)

    async def list_environments(self) -> list:
        # Routes on @RequestMapping(params = "offset") with offset/size/sortBy required — the
        # previous bare GET matched no handler at all (the long-standing "list_environments hits
        # the wrong endpoint" gap, root-caused 2026-07-13).
        result = await self.get("/rest/api/environments", params=self._LOOKUP_PAGING)
        return result if isinstance(result, list) else result.get("content", result)

    async def get_environment(self, env_id: str) -> dict:
        return await self.get(f"/rest/api/environments/{env_id}")

    async def create_environment(self, name: str, url: str, env_type: str = "Web",
                                 description: str = "") -> dict:
        # POST /rest/api/environments (LookupEnvironmentController). The URL lives in `value` —
        # an Environment is what execute_bot's executionConfiguration.baseUrl must reference (by
        # environmentId), so this is the missing piece when the app-under-test has no env yet.
        return await self.post("/rest/api/environments", json={
            "name": name, "value": url, "type": env_type, "description": description,
        })

    async def list_parameters(self) -> list:
        result = await self.get("/rest/api/parameters")
        return result if isinstance(result, list) else result.get("content", result)

    async def list_profiles(self) -> list:
        result = await self.get("/rest/api/profiles")
        return result if isinstance(result, list) else result.get("content", result)

    # --- Execution lookups (the source of valid ExecutionConfiguration values) ---
    # These are exactly what the frontend's Run TestBot dialog queries to populate its
    # grid/browser/environment dropdowns (lookup-grid/-browser/-execution-type controllers,
    # all config-services). An ExecutionConfiguration should only ever be assembled from
    # values returned here — never invented.
    # LookupGridController routes its list on @GetMapping(params = "offset") with offset/size/
    # sortBy all REQUIRED (same routing-key pattern as CommonFunctions) — a bare GET 400s.
    _LOOKUP_PAGING = {"offset": 0, "size": 200, "sortBy": "name", "orderBy": "ASC"}

    async def list_grids(self) -> list:
        result = await self.get("/rest/api/grids", params=self._LOOKUP_PAGING)
        grids = result if isinstance(result, list) else result.get("content", result)
        return _annotate_dead_grid_status(_redact_grid_credentials(grids))

    async def get_grid(self, grid_id: str) -> dict:
        grid = await self.get(f"/rest/api/grids/{grid_id}")
        return _annotate_dead_grid_status(_redact_grid_credentials(grid))

    async def list_browsers(self) -> list:
        result = await self.get("/rest/api/browsers", params=self._LOOKUP_PAGING)
        return result if isinstance(result, list) else result.get("content", result)

    async def list_execution_types(self) -> list:
        result = await self.get("/rest/api/execution-types")
        return result if isinstance(result, list) else result.get("content", result)

    async def get_grid_capabilities(self, grid_id: str, testing_type: str = "Web",
                                    browser: str = None) -> dict:
        """
        One-shot lookup of everything execute_bot's config needs for a grid: valid platforms
        (osType), browsers, resolutions, and (if a browser is given) its versions. Wraps the
        LookupGridController /provider/{gridId}/* endpoints — these live on config-services
        ONLY; guessing them via call_api on other services 404s.
        """
        platforms = await self.get(f"/rest/api/grids/provider/{grid_id}/platforms",
                                   params={"testingType": testing_type})
        platform = platforms[0] if isinstance(platforms, list) and platforms else None
        out = {"platforms": platforms}
        if platform:
            out["browsers"] = await self.get(f"/rest/api/grids/provider/{grid_id}/browsers",
                                             params={"platform": platform})
            out["resolutions"] = await self.get(f"/rest/api/grids/provider/{grid_id}/resolutions",
                                                params={"platform": platform})
            if browser:
                out["browserVersions"] = await self.get(
                    f"/rest/api/grids/provider/{grid_id}/browserVersions",
                    params={"browser": browser, "platform": platform})
        return out

    # --- Global Parameters ---
    # GlobalParametersController stores a project's parameters as ONE document
    # (GlobalParameter.customProperties: List<KeyValuePair>), not one row per parameter.
    async def list_global_parameters(self) -> dict:
        return await self.get("/rest/api/globalParameters")

    async def search_global_parameters(self, name: str = None) -> list:
        # Always includes the 3 default system params (baseUrl/timeout/waitForElementTimeout)
        # plus any custom properties, per GlobalParametersController.searchByName(). The `name`
        # query param is a REQUIRED @RequestParam server-side (no `required=false`) — omitting it
        # entirely 400s, even though an empty string is treated as "match everything." Confirmed
        # live: calling with no params returned "Invalid request parameters."
        return await self.get("/rest/api/globalParameters/search", params={"name": name or ""})

    async def add_global_parameter(self, name: str, value: str, description: str = None) -> dict:
        # SAFETY-CRITICAL: PUT/POST replaces the ENTIRE customProperties list server-side — it is
        # not a per-item patch (confirmed by reading GlobalParametersController.java directly: the
        # handler assigns whatever list the caller sends, it never merges). Sending only the new
        # property would silently wipe every other existing global parameter. This method GETs the
        # current document, appends the new property to customProperties in memory, then POSTs the
        # whole merged document back — the same defensive pattern as
        # AssetClient/update_common_function for the identical trap found in that entity.
        current = await self.list_global_parameters()
        properties = current.get("customProperties") or []
        properties.append({"name": name, "value": value, "description": description})
        current["customProperties"] = properties
        return await self.post("/rest/api/globalParameters", json=current)

    async def check_global_parameter_usage(self, custom_property_id: str) -> dict:
        return await self.get(f"/rest/api/globalParameters/custom/{custom_property_id}/usage")

    async def flatten_and_delete_global_parameter(self, custom_property_id: str) -> dict:
        # This is the ONLY deletion path — there is no plain DELETE endpoint. It converts every
        # test-step reference (type=2) to a literal value first, then removes the property.
        return await self.post(f"/rest/api/globalParameters/custom/{custom_property_id}/flatten")

    # --- Vault (config-services' own vault — separate from managed_testing_client's mtaf-core vault) ---
    # VaultSecretController reads "organizationId"/"projectId" headers, NOT the "org-id" this
    # client sends by default — a header-naming inconsistency within ahq-config-services itself,
    # confirmed by reading GlobalParametersController (uses "org-id") and VaultSecretController
    # (uses "organizationId") side by side. Every vault call below must pass the override.
    def _vault_headers(self) -> dict:
        return {"organizationId": self._credentials.org_id}

    async def list_config_vault_secrets(self) -> list:
        return await self.get("/rest/api/vault/list", extra_headers=self._vault_headers())

    async def get_config_vault_secret(self, secret_id: str) -> dict:
        return await self.get(f"/rest/api/vault/{secret_id}", extra_headers=self._vault_headers())

    async def create_config_vault_secret(self, name: str, value: str, description: str = None) -> dict:
        payload = {"name": name, "value": value}
        if description:
            payload["description"] = description
        return await self.post("/rest/api/vault", json=payload, extra_headers=self._vault_headers())

    async def update_config_vault_secret(self, secret_id: str, value: str = None, description: str = None) -> dict:
        payload = {}
        if value is not None:
            payload["value"] = value
        if description is not None:
            payload["description"] = description
        return await self.put(f"/rest/api/vault/{secret_id}", json=payload, extra_headers=self._vault_headers())

    async def delete_config_vault_secret(self, secret_id: str) -> dict:
        return await self.delete(f"/rest/api/vault/{secret_id}", extra_headers=self._vault_headers())

    # Deliberately NOT wrapped: GET /rest/api/vault/getByName/{name} and GET /rest/api/vault/getById/{id}
    # both return the DECRYPTED PLAINTEXT secret value directly. Exposing either as an MCP tool would
    # put real credentials into the conversation transcript — the same policy already established for
    # mtaf-core's vault (see managed_testing_client.py's list_vault_secrets docstring).


_GRID_STATUS_NOT_LIVE = (
    "NOT REPORTED — the platform stores no heartbeat for this grid (lastSeen is null), so this "
    "field is not a live connectivity signal and must not be used to choose an execution target. "
    "For the local agent use check_local_agent_status; for a cloud grid, submit the run."
)


def _annotate_dead_grid_status(grids):
    """
    `status`/`activeSessionCount`/`lastSeen` are declared on the grid document but nothing writes
    them: every grid in a live project reads OFFLINE / 0 / null at once, including a grid the user
    has just brought up and one that goes on to run a suite successfully. Reported verbatim, that
    is worse than absent — a caller picking a target reads a uniform "OFFLINE" as real and either
    avoids a working grid or distrusts the whole response.

    A null `lastSeen` is the tell that nothing ever populated the row, so the replacement is
    conditional on it: if the platform ever starts writing heartbeats, the real status flows
    through untouched and this annotation disappears on its own.
    """
    if not isinstance(grids, list):
        return grids
    for grid in grids:
        if isinstance(grid, dict) and grid.get("lastSeen") is None and "status" in grid:
            grid["reportedStatus"] = grid["status"]
            grid["status"] = _GRID_STATUS_NOT_LIVE
    return grids


# Values the platform stores when a grid needs no credentials. Masking these would be noise, and
# would hide the useful fact that the grid is open.
_CREDENTIAL_PLACEHOLDERS = frozenset({"", "no_key", "no_user", "no_secret", "none", "null"})

_REDACTED = "[redacted — the MCP layer never returns grid credentials]"


def _looks_like_a_secret(value) -> bool:
    return isinstance(value, str) and value.strip().lower() not in _CREDENTIAL_PLACEHOLDERS


def _strip_userinfo(url: str) -> str:
    """
    Remove a `user:pass@` prefix from a grid URL, keeping the scheme, host, port and path so the
    grid is still identifiable. Hand-parsed rather than via urlsplit/urlunsplit so a URL this
    client does not fully understand is returned unchanged instead of silently reassembled.
    """
    marker = "://"
    scheme_end = url.find(marker)
    if scheme_end == -1:
        return url
    rest_start = scheme_end + len(marker)
    authority_end = len(url)
    for sep in ("/", "?", "#"):
        found = url.find(sep, rest_start)
        if found != -1:
            authority_end = min(authority_end, found)
    authority = url[rest_start:authority_end]
    at = authority.rfind("@")
    if at == -1:
        return url
    return url[:rest_start] + authority[at + 1:] + url[authority_end:]


def _redact_grid_credentials(grids):
    """
    Strip working credentials out of grid records before they reach the model.

    A grid document carries `accessKey` in plaintext and repeats it inside `url` as
    `https://user:key@hub.browserstack.com/wd/hub`. Returned verbatim, every list_grids call
    writes usable BrowserStack/TestingBot credentials into the conversation transcript, which is
    then stored, and for hosted deployments leaves the machine entirely.

    Nothing needs them: execute_bot selects a grid by `gridId` and the platform attaches its own
    credentials server-side. `username` is deliberately kept — it identifies the account and is
    useless on its own once the key is gone.
    """
    if isinstance(grids, dict):
        return _redact_grid_credentials([grids])[0]
    if not isinstance(grids, list):
        return grids
    for grid in grids:
        if not isinstance(grid, dict):
            continue
        if _looks_like_a_secret(grid.get("accessKey")):
            grid["accessKey"] = _REDACTED
        url = grid.get("url")
        if isinstance(url, str):
            stripped = _strip_userinfo(url)
            if stripped != url:
                grid["url"] = stripped
                grid["urlCredentials"] = _REDACTED
    return grids
