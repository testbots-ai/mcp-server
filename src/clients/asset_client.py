import re

from src.clients.base_client import BaseAhqClient
from src.config.ahq_services import ASSET_SVC
from src.config.credentials import decode_ahq_token

# CommonFunctionController masks these templates' sensitive param (type-0 literal values become
# all-asterisks) on every GET — mirrored here so update_common_function can refuse to write a
# mask back over the real stored value. Keep in sync with ENCRYPTED_SENSITIVE_PARAM_KEY there.
_ENCRYPTED_SENSITIVE_PARAM_KEY = {
    "template-id-105": "password",
    "template-id-98": "text",
}
_MASK_RE = re.compile(r"^\*+$")


class AssetClient(BaseAhqClient):
    def __init__(self, credentials=None, http_client=None):
        super().__init__(ASSET_SVC, credentials, http_client)

    async def validate_token(self) -> dict:
        claims = decode_ahq_token(self._credentials.api_token)
        org_name = claims.get("organizationName", "unknown org")
        created_by = claims.get("createdByUserId", "unknown")
        return {"name": org_name, "userId": created_by, "tokenType": claims.get("tokenType", "UNKNOWN")}

    # --- Websites ---
    async def list_websites(self) -> list:
        result = await self.get("/rest/api/websites/list", params={"size": 1000})
        return result if isinstance(result, list) else result.get("content", result)

    async def search_websites(self, name: str) -> list:
        return await self.get("/rest/api/websites/list/search", params={"search": name})

    async def create_website(self, name: str, url: str) -> dict:
        return await self.post("/rest/api/websites", json={"name": name, "url": url})

    async def get_website(self, website_id: str) -> dict:
        return await self.get(f"/rest/api/websites/{website_id}")

    # --- Pages ---
    async def list_pages(self, website_id: str) -> list:
        # Returns an ObjectRepository: {"name": "Object Repository", "pages": [...]} — NOT
        # {"content": [...]} like the other list_* endpoints in this service.
        result = await self.get(
            f"/rest/api/websites/{website_id}/pages/list",
            params={"size": 1000},
        )
        return result if isinstance(result, list) else result.get("pages", result)

    async def create_page(self, website_id: str, name: str, url: str = "") -> dict:
        return await self.post(
            f"/rest/api/websites/{website_id}/pages",
            json={"pageName": name, "pageUrl": url},
        )

    async def get_page_by_url(self, website_id: str, url: str) -> dict:
        return await self.get(
            "/rest/api/locators/byPage",
            params={"url": url},
            extra_headers={"websiteId": website_id},
        )

    # --- Locators ---
    async def add_locators(self, page_id: str, website_id: str, page_url: str, page_name: str, locators: list) -> dict:
        # pushSpyElements expects a raw JSON array of full Page objects (each with its locators
        # embedded), NOT {"pageId": ..., "elements": [...]} - confirmed against the real
        # LocatorController.pushSpyElements(@RequestBody List<Page> pages). It upserts by
        # pageUrl match (findBy...PageUrl...), so page_url must be included or this creates a
        # duplicate page instead of merging into the one just created via create_page.
        #
        # ActionLibraryServices.getLocatorBy()/getLocatorValue() (ahq-actions-commons) has a real
        # bug: two separate `if` statements instead of `if/else if`, so it calls .isEmpty() on
        # locator.getLocateBy() even after already detecting it's null, throwing an NPE at
        # execution time - confirmed live, execution failed on step 2 with exactly this NPE.
        # This only bites locators created via locationStrategies alone (the modern, correct way,
        # which is all pushSpyElements needs) - the UI's own recorder/manual-entry flow apparently
        # always double-writes the deprecated singular locateBy/locatorValue fields too, so it
        # never trips this. Mirror that behavior here as a defensive workaround until the real bug
        # is fixed upstream: populate locateBy/locatorValue from the selected (or first) strategy.
        for loc in locators:
            strategies = loc.get("locationStrategies") or []
            if strategies and not loc.get("locateBy"):
                chosen = next((s for s in strategies if s.get("selected")), strategies[0])
                loc["locateBy"] = chosen.get("locateBy", "")
                loc["locatorValue"] = chosen.get("locatorValue", "")

        page_payload = {
            "pageId": page_id,
            "pageName": page_name,
            "pageUrl": page_url,
            "websiteId": website_id,
            "locators": locators,
        }
        result = await self.post(
            "/rest/api/locators/pushSpyElements",
            json=[page_payload],
            extra_headers={"websiteId": website_id},
            timeout=60,
        )
        # pushSpyElements doesn't echo the created locator IDs, but every caller needs them next
        # (to build script steps) — previously forcing a get_page_by_url round-trip that returned
        # the entire page document. Resolve name->locatorId here instead.
        created = []
        try:
            page = await self.get_page_by_url(website_id, page_url)
            for loc in (page.get("locators") or []):
                created.append({"locatorName": loc.get("locatorName"),
                                "locatorId": loc.get("locatorId") or loc.get("id")})
        except Exception:
            pass  # locator IDs are a convenience — never fail the add over the lookup
        return {"result": result, "pageId": page_id, "locators": created}

    # --- Common Functions (User Test Steps) ---
    async def list_common_functions(self, name: str = None) -> list:
        # The list handler only routes when "offset" is present in the query string —
        # @RequestMapping(method = GET, params = "offset") on CommonFunctionController.list().
        # Omitting offset doesn't page differently, it 404s/405s because NO handler matches.
        params = {"offset": 0, "size": 1000}
        if name:
            params["name"] = name
        result = await self.get("/rest/api/commonFunctions", params=params)
        return result.get("content", result) if isinstance(result, dict) else result

    async def get_common_function(self, common_function_id: str) -> dict:
        return await self.get(f"/rest/api/commonFunctions/{common_function_id}")

    async def create_common_function(
        self,
        name: str,
        website_id: str,
        status: str,
        return_type: dict,
        steps: list = None,
        parameters: list = None,
        description: str = None,
    ) -> dict:
        payload = {
            "name": name,
            "websiteId": website_id,
            "status": status,
            "returnType": return_type,
            "testSteps": steps or [],
            "parameters": parameters or [],
            "description": description or "",
        }
        return await self.post("/rest/api/commonFunctions", json=payload)

    async def update_common_function(self, common_function_id: str, **changes) -> dict:
        # SAFETY-CRITICAL: PUT /rest/api/commonFunctions/{id} is a full-document replace
        # (commonFunctionRepo.save(requestBody) — the server only forces commonFunctionId back
        # onto the body; it does NOT preserve organizationId/projectId/createdBy/testSteps/
        # parameters/returnType from the existing document). A partial body silently wipes every
        # omitted field AND orphans the function from its org. So: GET the full document first,
        # shallow-merge the requested changes on top, PUT the merged whole back. This is the fix
        # for the wipe-on-rename incident (project_user_test_step_rename_fix), baked in so it
        # cannot be bypassed.
        current = await self.get_common_function(common_function_id)
        merged_steps = changes.get("testSteps", current.get("testSteps"))
        if "testSteps" not in changes and self._has_masked_encrypted_value(merged_steps):
            raise ValueError(
                "This User Test Step contains an encrypted-text step whose literal value the "
                "server masks (returns '****') on every read — writing the fetched document back "
                "would permanently overwrite the real stored value with asterisks. Pass new "
                "testSteps (re-supplying the real encrypted value, or a vault/type-7 reference) "
                "to update this function, or leave it unchanged."
            )
        current.update(changes)
        return await self.put(f"/rest/api/commonFunctions/{common_function_id}", json=current)

    @staticmethod
    def _has_masked_encrypted_value(test_steps) -> bool:
        for step in test_steps or []:
            sensitive_key = _ENCRYPTED_SENSITIVE_PARAM_KEY.get(step.get("templateId"))
            if not sensitive_key:
                continue
            for param in step.get("parameters") or []:
                if param.get("key") != sensitive_key:
                    continue
                value = param.get("value")
                if not isinstance(value, dict):
                    continue
                tvp_value = value.get("value")
                if value.get("type") == 0 and isinstance(tvp_value, str) and _MASK_RE.match(tvp_value):
                    return True
        return False

    async def update_locator(
        self, website_id: str, page_id: str, locator_id: str, locator_name: str, locator_type: str, locate_by: str, locator_value: str
    ) -> dict:
        # add_locators (pushSpyElements) only ADDS locators that don't already match an existing
        # one by locationStrategies value (LocatorController.mergeLocators/strategiesMatch) — it
        # silently no-ops for anything that already exists, never updating fields on it. This is
        # the only way to actually fix an already-created locator (e.g. backfilling
        # locateBy/locatorValue on one created before that fix existed).
        body = {
            "locatorId": locator_id,
            "locatorName": locator_name,
            "locatorType": locator_type,
            "locateBy": locate_by,
            "locatorValue": locator_value,
            "locationStrategies": [{"locateBy": locate_by, "locatorValue": locator_value, "selected": True}],
        }
        return await self.put(f"/rest/api/websites/{website_id}/pages/{page_id}/locator/{locator_id}", json=body)

    # --- Self-healing locators ---
    async def list_broken_locators(self) -> list:
        # LocatorController's aggregated dashboard view — every Locator across every page with
        # broken == true (set by the executor's brokenLocatorReporter callback once every
        # locationStrategy fails during a real execution). Already live on the backend; this is
        # the first tool to actually call it. Confirmed live: the response is
        # {"items": [...], "status": 200, "count": N} — NOT {"content": [...]} like the other
        # paginated list_* endpoints in this client.
        result = await self.get("/rest/api/locators/broken")
        items = result if isinstance(result, list) else result.get("items", result)
        if isinstance(items, list) and not items:
            # An empty list here is NOT "nothing is broken". Only the cloud path calls
            # setBrokenLocatorReporter (ahq-background-v2-services' TestRemoteExecutionRepository);
            # test-local-execution-services has zero occurrences, so a locator that fails during a
            # local-agent run never sets broken=true and never reaches this endpoint. Returning a
            # bare [] reads as an all-clear and sends the caller looking somewhere else entirely.
            return {
                "items": [],
                "count": 0,
                "note": (
                    "No locator is FLAGGED broken — this is not the same as 'no locator is "
                    "broken'. Only cloud executions report breakages; local-agent runs never "
                    "set the flag, so if the failing run used the AHQ Local Agent this list is "
                    "expected to be empty regardless of what actually broke. Diagnose from the "
                    "execution report's per-step errors, or re-crawl the page with crawl_url / "
                    "get_page_by_url and compare against the locator's stored strategies."
                ),
            }
        return items

    async def get_page_by_locator_id(self, website_id: str, locator_id: str) -> dict:
        # Confirmed live: this endpoint 400s ("Required header 'websiteId' is not present") without
        # the same websiteId header get_page_by_url already sends — the query param alone isn't enough.
        return await self.get(
            "/rest/api/locators/byLocatorId",
            params={"locatorId": locator_id},
            extra_headers={"websiteId": website_id},
        )

    async def apply_locator_strategy(self, website_id: str, locator_id: str, new_strategy: dict) -> dict:
        # Self-healing apply path. Deliberately does NOT call update_locator above — that method
        # always collapses locationStrategies to a single entry, which would destroy every
        # existing fallback strategy. Here the new strategy is kept alongside every existing one
        # (never discarded) — but it MUST be placed FIRST in the array, not appended.
        #
        # Confirmed live against PageService.normalizeStrategySelection (ahq-asset-services): it
        # marks a strategy "selected" by matching locateBy TYPE only ("css" == "css"), not the
        # actual locatorValue — so two same-type strategies both end up selected=true regardless
        # of what this client sends. ActionLibraryServices.getElementByWithFallback then breaks
        # in a specific way: getSelectedLocationStrategy() takes stream().filter(selected).
        # findFirst() (array order wins among selected=true entries), while
        # getFallbackLocationStrategies() excludes every selected=true entry from the fallback
        # pool. Net effect: whichever same-type strategy sits FIRST in the array is the only one
        # ever tried — anything after it is neither the chosen primary nor an eligible fallback,
        # i.e. permanently unreachable. Putting the new (already validated) strategy first is the
        # only way, without an ahq-asset-services change, to guarantee it's actually exercised at
        # execution time.
        page = await self.get_page_by_locator_id(website_id, locator_id)
        page_id = page.get("pageId")
        locator = next((l for l in (page.get("locators") or []) if l.get("locatorId") == locator_id), None)
        if locator is None:
            raise ValueError(
                f"Locator {locator_id} was not found on its own page — it may already have been "
                "deleted or archived."
            )

        kept = [{**s, "selected": False} for s in (locator.get("locationStrategies") or [])]
        # Whitelist fields — heal_locator's candidates carry a "confidence" score alongside
        # locateBy/locatorValue that has no place in the backend's LocationStrategy shape.
        clean_new_strategy = {
            "locateBy": new_strategy.get("locateBy", ""),
            "locatorValue": new_strategy.get("locatorValue", ""),
            "selected": True,
        }
        strategies = [clean_new_strategy] + kept

        body = {
            "locatorId": locator_id,
            "locatorName": locator.get("locatorName"),
            "locatorType": locator.get("locatorType"),
            "locateBy": clean_new_strategy["locateBy"],
            "locatorValue": clean_new_strategy["locatorValue"],
            "locationStrategies": strategies,
        }
        return await self.put(f"/rest/api/websites/{website_id}/pages/{page_id}/locator/{locator_id}", json=body)
