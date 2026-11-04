import copy
import re

from src.clients.base_client import BaseAhqClient
from src.config.ahq_services import MANAGED_TEST_SVC


def _unwrap(result):
    """mtaf-core list responses wrap items in a `data` field (not `content` like the UI-test services)."""
    return result.get("data", result) if isinstance(result, dict) else result


_PERF_RUN_FIELDS = (
    "id", "name", "status", "created", "updated", "overallStatus", "slaVerdict",
    "failureReason", "skippedRequests", "estimatedVirtualUsers", "estimatedDurationMinutes",
)

_MAX_ENDPOINTS = 20


def _seconds(duration) -> float:
    """A java.time.Duration as Jackson writes it: numeric seconds, or ISO-8601 like PT0.25S."""
    if isinstance(duration, (int, float)):
        return float(duration)
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", str(duration or ""))
    if not match:
        return 0.0
    h, m, sec = match.groups()
    return int(h or 0) * 3600 + int(m or 0) * 60 + float(sec or 0)


def _flatten_endpoints(workflow_metrics) -> list:
    endpoints = []
    for wf in workflow_metrics or []:
        if not isinstance(wf, dict):
            continue
        for req in wf.get("apiRequestMetrics") or []:
            if isinstance(req, dict):
                endpoints.append({
                    "workflow": wf.get("name"),
                    "name": req.get("name"),
                    **(req.get("overallStatus") or {}),
                })
    endpoints.sort(key=lambda e: (e.get("errorCount") or 0, _seconds(e.get("perc95ResponseTime"))), reverse=True)
    return endpoints[:_MAX_ENDPOINTS]


# POST /rest/api/performance-bots with an id REPLACES the whole bot (only created/createdBy are
# carried over server-side), so an edit is GET-merge-POST of the full document. These are the
# response-only fields the frontend strips before saving (callMtafTestBotCreateApi's Omit<>).
_PERF_BOT_RESPONSE_ONLY = (
    "workflowsViewList", "running", "hasDeletedOrEmptyWorkflows", "message",
    "created", "createdBy", "updated", "updatedBy",
)

# Friendly SLA argument names -> SlaRequirement fields.
PERF_SLA_FIELDS = {
    "p95_ms": "p95ResponseTimeMs",
    "median_ms": "medianResponseTimeMs",
    "error_percent": "errorPercentage",
    "throughput": "throughput",
    "network_latency_ms": "networkLatencyMs",
    "users": "numberOfUsers",
}

_ACTIVE_RUN_STATUSES = {"RUNNING", "ENQUEUED", "STARTING", "PROCESSING", "INITIALIZING"}

# The only ramp type the editor produces: 1 = "Ramp And Hold" (PerformanceBotTypeEnum).
_RAMP_AND_HOLD = 1


def _current_sla(bot: dict) -> dict | None:
    for sla in bot.get("slaRequirements") or []:
        if isinstance(sla, dict) and (sla.get("current") or sla.get("isCurrent")):
            return sla
    return None


def _sla_view(sla: dict | None) -> dict | None:
    if not sla:
        return None
    view = {k: sla.get(f) for k, f in PERF_SLA_FIELDS.items() if sla.get(f) is not None}
    return view or None


def _stages_view(bot: dict) -> list:
    stages = ((bot.get("performanceParameters") or {}).get("rampStrategies")) or []
    return [
        {"users": s.get("numberOfUsers"), "ramp_up_seconds": s.get("rampToInSeconds"),
         "hold_seconds": s.get("rampTimeInSeconds"),
         **({"type": "Iterations"} if s.get("type") == 0 else {})}
        for s in stages if isinstance(s, dict)
    ]


def _bot_view(bot: dict) -> dict:
    return {"sla": _sla_view(_current_sla(bot)), "load_stages": _stages_view(bot),
            "environment": bot.get("selectedEnvironment") or None}


class ManagedTestingClient(BaseAhqClient):
    """
    Handles API testing and performance testing (mtaf-core) — a separate flow from UI bot testing,
    used for REST/GraphQL request validation, chained-workflow testing, and JMeter-backed load testing.
    """

    def __init__(self, credentials=None, http_client=None):
        super().__init__(MANAGED_TEST_SVC, credentials, http_client)

    # --- API Collections v2 ---
    async def list_api_collections(self) -> list:
        return _unwrap(await self.get("/rest/api/v2/collections/list"))

    async def get_api_collection(self, collection_id: str) -> dict:
        return await self.get(f"/rest/api/v2/collections/{collection_id}")

    async def create_api_collection(self, name: str, description: str = None, variables: list = None) -> dict:
        payload = {"name": name}
        if description:
            payload["description"] = description
        if variables:
            payload["variables"] = variables
        return await self.post("/rest/api/v2/collections", json=payload)

    # --- API Requests v2 ---
    async def list_api_requests(self) -> list:
        return _unwrap(await self.get("/rest/api/v2/requests/list"))

    async def get_api_request(self, request_id: str) -> dict:
        return await self.get(f"/rest/api/v2/requests/{request_id}")

    async def create_api_request(
        self,
        name: str,
        method: str,
        url: str,
        collection_id: str = None,
        query_params: list = None,
        header_params: list = None,
        body_params=None,
    ) -> dict:
        payload = {"name": name, "method": method, "url": url}
        if collection_id:
            payload["collectionId"] = collection_id
        if query_params:
            payload["queryParams"] = query_params
        if header_params:
            payload["headerParams"] = header_params
        if body_params is not None:
            payload["bodyParams"] = body_params
        return await self.post("/rest/api/v2/requests", json=payload)

    async def test_api_request(
        self, request: dict, variables: dict = None, data_row: dict = None, environment: str = None
    ) -> dict:
        payload = {
            "request": request,
            "variables": variables or {},
            "dataRow": data_row or {},
        }
        if environment:
            payload["environment"] = environment
        return await self.post("/rest/api/v2/requests/test", json=payload, timeout=60)

    # --- Import ---
    async def import_curl(
        self, commands: str, save: bool = True, collection_name: str = "cURL Import", collection_id: str = None
    ) -> dict:
        params = {"save": save, "collectionName": collection_name}
        if collection_id:
            params["collectionId"] = collection_id
        # base_client.post() always sends a json body — curl import needs a raw text/plain body instead.
        return await self._request(
            "POST",
            f"{self._base}/rest/api/v2/import/curl",
            params=params,
            content=commands,
            extra_headers={"Content-Type": "text/plain"},
            timeout=30,
        )

    async def import_postman(self, collection: dict, save: bool = True) -> dict:
        return await self.post(
            "/rest/api/v2/import/postman", json=collection, params={"save": save}, timeout=30
        )

    # --- Workflows ---
    async def list_workflows(self) -> list:
        return _unwrap(await self.get("/rest/api/workflows/list"))

    async def get_workflow(self, workflow_id: str) -> dict:
        return await self.get(f"/rest/api/workflows/list/{workflow_id}")

    async def create_workflow(self, name: str, description: str = None, workflow_list: list = None) -> dict:
        payload = {"name": name, "workflowList": workflow_list or []}
        if description:
            payload["description"] = description
        return await self.post("/rest/api/workflows", json=payload)

    async def test_workflow(
        self, name: str, api_requests: list, description: str = None, load_ratio: float = None
    ) -> dict:
        payload = {"name": name, "apiRequests": api_requests}
        if description:
            payload["description"] = description
        if load_ratio:
            payload["loadRatio"] = load_ratio
        return await self.post("/rest/api/workflows/test", json=payload, timeout=60)

    # --- Performance Bots ---
    async def list_performance_bots(self) -> list:
        return _unwrap(await self.get("/rest/api/performance-bots/list"))

    async def get_performance_bot(self, bot_id: str) -> dict:
        return await self.get(f"/rest/api/performance-bots/{bot_id}")

    async def run_performance_bot(self, bot_id: str) -> dict:
        return await self.post(f"/rest/api/performance-bots/run/{bot_id}")

    async def update_performance_bot(
        self, bot_id: str, sla: dict = None, load_stages: list = None,
        environment: str = None, confirm: bool = False,
    ) -> dict:
        """
        Changes the current SLA, the load stages and/or the collection environment of an existing
        bot. Without confirm it only returns the before/after preview; nothing is saved.
        """
        bot = _unwrap(await self.get(f"/rest/api/performance-bots/{bot_id}"))
        if not isinstance(bot, dict) or not bot.get("id"):
            return {"error": "Performance bot not found."}

        latest = await self.list_performance_runs(bot_id, 0, 1)
        if latest and isinstance(latest[0], dict) and str(latest[0].get("status", "")).upper() in _ACTIVE_RUN_STATUSES:
            return {"error": f"{bot.get('name')} is running now. Change it after the run ends, or stop the run first."}

        updated = copy.deepcopy(bot)
        if sla is not None:
            slas = [x for x in (updated.get("slaRequirements") or []) if isinstance(x, dict)]
            current = _current_sla({"slaRequirements": slas})
            if current is None:
                current = {"current": True}
                slas.append(current)
            for key, value in sla.items():
                field = PERF_SLA_FIELDS[key]
                if value is None:
                    current.pop(field, None)
                else:
                    current[field] = value
            current["current"] = True
            current.pop("isCurrent", None)
            if not any(current.get(f) is not None for f in PERF_SLA_FIELDS.values()):
                slas.remove(current)
            updated["slaRequirements"] = slas
        if load_stages is not None:
            params = updated.get("performanceParameters") or {}
            params["rampStrategies"] = [
                {"type": _RAMP_AND_HOLD, "hold": False, "numberOfUsers": st["users"],
                 "rampToInSeconds": st["ramp_up_seconds"], "rampTimeInSeconds": st["hold_seconds"]}
                for st in load_stages
            ]
            updated["performanceParameters"] = params
        if environment is not None:
            name = environment.strip()
            if name:
                known = await self._collection_environment_names(updated.get("apiCollectionIds") or [])
                if known and name.lower() not in {k.lower() for k in known}:
                    return {"error": f"No environment named '{name}' on this bot's API collections. "
                                     f"Available: {', '.join(sorted(known))}."}
            updated["selectedEnvironment"] = name or None

        before, after = _bot_view(bot), _bot_view(updated)
        changes = {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]}
        if not changes:
            return {"saved": False, "message": "Nothing to change - the bot already has these settings.", "current": before}
        if not confirm:
            return {"saved": False, "preview": True, "bot": bot.get("name"), "changes": changes,
                    "message": "Nothing saved yet. Show these changes to the user and call again with confirm=true once they agree."}

        for key in _PERF_BOT_RESPONSE_ONLY:
            updated.pop(key, None)
        result = await self.post("/rest/api/performance-bots", json=updated)
        status = result.get("status") if isinstance(result, dict) else None
        if status not in (None, 200):
            return {"error": result.get("message") or f"Save failed with status {status}."}
        return {"saved": True, "bot": bot.get("name"), "changes": changes,
                "message": (result.get("message") if isinstance(result, dict) else None) or "Performance bot updated."}

    async def _collection_environment_names(self, collection_ids: list) -> set:
        # Fail-open: an unreadable collection or one with no environments leaves the name
        # unchecked rather than blocking the edit. The backend ignores an unknown name silently
        # (V2VariableResolver falls back to initial values), which is why it is checked at all.
        names = set()
        for cid in collection_ids:
            try:
                col = _unwrap(await self.get(f"/rest/api/v2/collections/{cid}"))
            except Exception:
                return set()
            for env in (col.get("environments") or []) if isinstance(col, dict) else []:
                if isinstance(env, dict) and env.get("name"):
                    names.add(env["name"])
        return names

    async def stop_performance_bot(self, bot_id: str) -> dict:
        return await self.post(f"/rest/api/performance-bots/stop/{bot_id}")

    async def list_performance_runs(
        self, bot_id: str, page: int = 0, size: int = 10, include_endpoints: bool = False
    ) -> list:
        # Newest first (sorted by `created` server-side). workflowMetrics nests per-request stats
        # under each workflow; it is flattened into `endpoints` only on request, worst first and
        # capped, so a plain history page stays small.
        result = _unwrap(await self.get(
            f"/rest/api/performance-metrics/paged/list/{bot_id}", params={"page": page, "size": size}
        ))
        runs = result.get("content", result) if isinstance(result, dict) else result
        if not isinstance(runs, list):
            return runs
        slim_runs = []
        for r in runs:
            if not isinstance(r, dict):
                slim_runs.append(r)
                continue
            slim = {k: r[k] for k in _PERF_RUN_FIELDS if r.get(k) is not None}
            if include_endpoints:
                slim["endpoints"] = _flatten_endpoints(r.get("workflowMetrics"))
            slim_runs.append(slim)
        return slim_runs

    async def get_performance_results(self, metrics_id: str, polling: bool = True) -> dict:
        return await self.get(
            f"/rest/api/performance-metrics/results/{metrics_id}", params={"polling": str(polling).lower()}
        )

    # --- Vault Secrets v2 ---
    # Metadata only (name/key) — deliberately not wrapping /resolve/{key}, which returns the
    # decrypted plaintext secret and shouldn't land in an LLM conversation transcript by default.
    async def list_vault_secrets(self) -> list:
        return _unwrap(await self.get("/rest/api/v2/vault/list"))
