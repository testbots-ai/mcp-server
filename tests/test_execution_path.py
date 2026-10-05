"""
Execution-path slice: create_test_bot / suite fixes / execute_bot / lookups.
Contracts from TestBotController.addTestBot, TestBotExecutionController.executeBot,
TestSuiteController (no /scripts endpoint — embedded testScripts), and
automationhq-frontend-v2's TestBotSchema + ExecutionConfigurationSchema (zod).
"""
import pytest
from pydantic import ValidationError

from src.clients.bundle import DEFAULT_BUNDLE
from src.clients.executor_client import ExecutorClient
from src.clients.test_mgmt_client import TestMgmtClient
from src.mcp_server import _dispatch
from src.schema.asset_kinds import ExecuteBotArgs, TestBotCreateArgs


# --- validators ---

def _valid_exec_config(**overrides):
    cfg = {"baseUrl": "env-uuid-1", "browser": "Chrome", "gridId": "grid-1",
           "browserVersion": "latest", "osType": "Grid OS"}
    cfg.update(overrides)
    return cfg


def test_execute_bot_args_accepts_minimal_valid_config():
    args = ExecuteBotArgs(bot_id="b1", execution_configuration=_valid_exec_config())
    assert args.execution_configuration.timeout == 60          # form default
    assert args.execution_configuration.waitForElementTimeout == 30
    assert args.execution_configuration.screenshotOnError is True


@pytest.mark.parametrize("missing", ["baseUrl", "browser", "gridId", "browserVersion", "osType"])
def test_execute_bot_args_rejects_missing_required(missing):
    cfg = _valid_exec_config()
    del cfg[missing]
    with pytest.raises(ValidationError):
        ExecuteBotArgs(bot_id="b1", execution_configuration=cfg)


def test_execute_bot_args_rejects_raw_url_as_base_url():
    # baseUrl is an ENVIRONMENT ID server-side; a raw URL survives enqueue and then kills the
    # run at report time with "Environment not found for this id" (found live 2026-07-13).
    with pytest.raises(ValidationError) as exc_info:
        ExecuteBotArgs(bot_id="b1",
                       execution_configuration=_valid_exec_config(baseUrl="https://app.example.com"))
    assert "Environment ID" in str(exc_info.value)


@pytest.mark.parametrize("field,value", [
    ("timeout", 0), ("timeout", 301),
    ("waitForElementTimeout", 0), ("waitForElementTimeout", 301),
    ("delayBetweenSteps", 31), ("numberOfRetries", 4),
])
def test_execute_bot_args_enforces_form_bounds(field, value):
    with pytest.raises(ValidationError):
        ExecuteBotArgs(bot_id="b1", execution_configuration=_valid_exec_config(**{field: value}))


def test_test_bot_create_args_requires_at_least_one_suite():
    with pytest.raises(ValidationError):
        TestBotCreateArgs(name="Bot", test_suites=[])
    TestBotCreateArgs(name="Bot", test_suites=[{"testSuiteId": "s1", "name": "Suite"}])


def test_test_bot_create_args_name_bounds():
    with pytest.raises(ValidationError):
        TestBotCreateArgs(name="", test_suites=[{"testSuiteId": "s1"}])
    with pytest.raises(ValidationError):
        TestBotCreateArgs(name="x" * 121, test_suites=[{"testSuiteId": "s1"}])


# --- client wiring ---

async def test_create_test_bot_body_shape(monkeypatch):
    client = TestMgmtClient()
    captured = {}

    async def fake_post(path, json=None, **kwargs):
        captured["path"] = path
        captured["json"] = json
        return {"id": "bot-1"}

    monkeypatch.setattr(client, "post", fake_post)
    await client.create_test_bot("My Bot", [{"testSuiteId": "s1", "name": "Suite"}])

    assert captured["path"] == "/rest/api/testbots"
    assert captured["json"]["name"] == "My Bot"
    assert captured["json"]["testSuites"] == [{"testSuiteId": "s1", "name": "Suite"}]
    assert "botType" not in captured["json"]  # server defaults to REGRESSION_TEST


async def test_add_scripts_to_suite_is_get_merge_put(monkeypatch):
    client = TestMgmtClient()
    calls = []

    async def fake_get_suite(suite_id):
        return {"testSuiteId": suite_id, "name": "S",
                "testScripts": [{"testScriptId": "old", "sequence": 1}]}

    async def fake_get_script(script_id):
        return {"testScriptId": script_id, "name": f"Script {script_id}", "status": "Ready"}

    async def fake_put(path, json=None, **kwargs):
        calls.append((path, json))
        return {"ok": True}

    monkeypatch.setattr(client, "get_suite", fake_get_suite)
    monkeypatch.setattr(client, "get_test_script", fake_get_script)
    monkeypatch.setattr(client, "put", fake_put)

    await client.add_scripts_to_suite("suite-1", ["new-1", "old"])  # "old" already attached

    path, body = calls[0]
    assert path == "/rest/api/suites/suite-1"
    ids = [s["testScriptId"] for s in body["testScripts"]]
    assert ids == ["old", "new-1"]              # merged, no duplicate of "old"
    assert body["testScripts"][1]["sequence"] == 2


async def test_execute_bot_sends_full_botexecution_body(monkeypatch):
    client = ExecutorClient()
    captured = {}

    async def fake_post(path, json=None, params=None, **kwargs):
        captured.update(path=path, json=json, params=params)
        return {"executionId": "e1"}

    monkeypatch.setattr(client, "post", fake_post)
    await client.execute_bot("bot-1", {"baseUrl": "u", "browser": "Chrome", "gridId": "g"},
                             name="Run 1", profile_id="p1")

    assert captured["path"] == "/rest/api/bots/bot-1/execute"
    assert captured["json"]["name"] == "Run 1"
    assert captured["json"]["profileId"] == "p1"
    assert captured["json"]["executionConfiguration"]["gridId"] == "g"
    assert captured["params"] is None  # partialExecution only sent when requested


async def test_get_execution_results_uses_detailed_results_path(monkeypatch):
    client = ExecutorClient()
    captured = {}

    async def fake_get(path, **kwargs):
        captured["path"] = path
        return {}

    monkeypatch.setattr(client, "get", fake_get)
    await client.get_execution_results("e1")
    assert captured["path"] == "/rest/api/bots/execution/e1/detailed-results"


# --- dispatch-level: defaults must actually reach the outgoing payload, not just validate ---
# (regression coverage for the bug where VALIDATORS[name](**args) built a fully-defaulted model
# only to check for errors, then discarded it — the raw, non-defaulted args dict is what used to
# reach the API. A caller that only supplies the hard-required fields used to get 0-second
# timeouts, no explicit wait, and closeBrowserAfterEachExecution=False, silently.)

async def _no_global_parameters():
    return {"customProperties": []}


async def test_dispatch_execute_bot_fills_in_omitted_defaults(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://some-cloud-grid.example.com/wd/hub"}

    async def fake_execute_bot(bot_id, execution_configuration, **kwargs):
        captured["execution_configuration"] = execution_configuration
        return {"executionId": "e1"}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", fake_execute_bot)

    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(),  # only the 5 hard-required fields
    }, DEFAULT_BUNDLE)

    cfg = captured["execution_configuration"]
    assert cfg["timeout"] == 60
    assert cfg["waitForElementTimeout"] == 30
    assert cfg["type"] == "Web"
    # NOT "Local Machine Resolution" — this is a cloud grid (see fake_get_grid above), and that
    # value is a real cloud grid like TestingBot rejects outright (confirmed live: 500, "Invalid
    # screen-resolution specified: Local Machine Resolution").
    assert "resolution" not in cfg
    assert cfg["targetBranchName"] == "main"
    assert cfg["closeBrowserAfterEachExecution"] is True


async def test_dispatch_execute_bot_defaults_resolution_only_for_local_grid(monkeypatch):
    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "http://localhost:4455/wd/hub"}

    captured = {}

    async def fake_execute_bot_locally(bot_id, execution_configuration, name=None):
        captured["execution_configuration"] = execution_configuration
        return {"message": "Job is enqueued."}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.local_exec, "execute_bot_locally", fake_execute_bot_locally)

    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(),
    }, DEFAULT_BUNDLE)

    assert captured["execution_configuration"]["resolution"] == "Local Machine Resolution"


async def test_dispatch_execute_bot_does_not_override_explicit_resolution(monkeypatch):
    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "http://localhost:4455/wd/hub"}

    captured = {}

    async def fake_execute_bot_locally(bot_id, execution_configuration, name=None):
        captured["execution_configuration"] = execution_configuration
        return {"message": "Job is enqueued."}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.local_exec, "execute_bot_locally", fake_execute_bot_locally)

    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(resolution="1920x1080"),
    }, DEFAULT_BUNDLE)

    assert captured["execution_configuration"]["resolution"] == "1920x1080"


# --- dispatch-level: local-grid executions must bypass the cloud executor entirely ---
# (regression coverage for the bug found by capturing a real browser HAR: the cloud has no way
# to deliver a job to a specific developer's own machine, so routing a local-grid execution
# through it leaves the job stuck ENQUEUED forever. The browser itself POSTs straight to
# localhost:9202 — this mirrors that.)

async def test_dispatch_execute_bot_routes_local_grid_directly_to_agent(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "http://localhost:4455/wd/hub"}

    async def fake_execute_bot_locally(bot_id, execution_configuration, name=None):
        captured["bot_id"] = bot_id
        captured["execution_configuration"] = execution_configuration
        return {"message": "Job is enqueued."}

    async def fake_cloud_execute_bot(*args, **kwargs):
        raise AssertionError("must not call the cloud executor for a local-grid execution")

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.local_exec, "execute_bot_locally", fake_execute_bot_locally)
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", fake_cloud_execute_bot)

    result = await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(gridId="local-grid-1"),
    }, DEFAULT_BUNDLE)

    assert captured["bot_id"] == "b1"
    assert captured["execution_configuration"]["gridId"] == "local-grid-1"
    assert result == {"message": "Job is enqueued."}


async def test_dispatch_execute_bot_rejects_local_grid_when_hosted(monkeypatch):
    # The hosted server can never reach a caller's own machine (localhost:9202 there is the
    # pod's own loopback) — must return a clean error instead of trying and hanging/refusing.
    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "http://localhost:4455/wd/hub"}

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("must not attempt local-agent execution from a hosted session")

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.local_exec, "execute_bot_locally", must_not_be_called)
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", must_not_be_called)

    result = await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(gridId="local-grid-1"),
    }, DEFAULT_BUNDLE, is_hosted=True)

    assert "error" in result
    assert "hosted MCP server can't reach" in result["error"]


async def test_dispatch_execute_bot_keeps_non_local_grid_on_cloud_path(monkeypatch):
    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://testingbot-grid.example.com/wd/hub"}

    async def fake_local_execute(*args, **kwargs):
        raise AssertionError("must not call the local agent for a non-local grid")

    captured = {}

    async def fake_cloud_execute_bot(bot_id, execution_configuration, **kwargs):
        captured["called"] = True
        return {"executionId": "e1"}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.local_exec, "execute_bot_locally", fake_local_execute)
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", fake_cloud_execute_bot)

    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(),
    }, DEFAULT_BUNDLE)

    assert captured.get("called") is True


async def test_dispatch_schedule_bot_recurring_fills_in_omitted_defaults(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://some-cloud-grid.example.com/wd/hub"}

    async def fake_create_scheduler(bot_id, name, emails, cron, execution_configuration):
        captured["execution_configuration"] = execution_configuration
        return {"schedulerId": "s1"}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.test_mgmt, "create_scheduler", fake_create_scheduler)

    await _dispatch("schedule_bot_recurring", {
        "bot_id": "b1", "name": "Nightly Run", "cron": "0 0 * * *", "emails": [],
        "execution_configuration": _valid_exec_config(),
    }, DEFAULT_BUNDLE)

    cfg = captured["execution_configuration"]
    assert cfg["timeout"] == 60
    assert cfg["type"] == "Web"
    assert cfg["closeBrowserAfterEachExecution"] is True
    assert "resolution" not in cfg  # non-local grid — see _fill_resolution_default


async def test_dispatch_schedule_bot_recurring_requires_name(monkeypatch):
    result = await _dispatch("schedule_bot_recurring", {
        "bot_id": "b1", "cron": "0 0 * * *",
        "execution_configuration": _valid_exec_config(),
    }, DEFAULT_BUNDLE)
    assert "error" in result
    assert "name" in result["error"]


async def test_dispatch_cancel_schedule_uses_real_scheduler_endpoint(monkeypatch):
    captured = {}

    async def fake_delete(scheduler_id):
        captured["scheduler_id"] = scheduler_id
        return {"success": True}

    monkeypatch.setattr(DEFAULT_BUNDLE.test_mgmt, "delete_scheduler", fake_delete)

    await _dispatch("cancel_schedule", {"schedule_id": "sched-1"}, DEFAULT_BUNDLE)

    assert captured["scheduler_id"] == "sched-1"


async def test_dispatch_update_schedule_passes_only_given_fields_through(monkeypatch):
    captured = {}

    async def fake_update(scheduler_id, bot_id, name, emails, cron, execution_configuration):
        captured.update(scheduler_id=scheduler_id, bot_id=bot_id, name=name, emails=emails,
                        cron=cron, execution_configuration=execution_configuration)
        return {"schedulerId": scheduler_id}

    monkeypatch.setattr(DEFAULT_BUNDLE.test_mgmt, "update_scheduler", fake_update)

    await _dispatch("update_schedule", {"schedule_id": "sched-1", "cron": "0 9 * * *"}, DEFAULT_BUNDLE)

    assert captured["scheduler_id"] == "sched-1"
    assert captured["cron"] == "0 9 * * *"
    assert captured["bot_id"] is None
    assert captured["name"] is None
    assert captured["execution_configuration"] is None


async def test_dispatch_update_schedule_processes_execution_configuration_when_given(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://some-cloud-grid.example.com/wd/hub"}

    async def fake_update(scheduler_id, bot_id, name, emails, cron, execution_configuration):
        captured["execution_configuration"] = execution_configuration
        return {"schedulerId": scheduler_id}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", lambda: _no_global_parameters())
    monkeypatch.setattr(DEFAULT_BUNDLE.test_mgmt, "update_scheduler", fake_update)

    await _dispatch("update_schedule", {
        "schedule_id": "sched-1",
        "execution_configuration": _valid_exec_config(),
    }, DEFAULT_BUNDLE)

    cfg = captured["execution_configuration"]
    assert cfg["timeout"] == 60
    assert cfg["type"] == "Web"


# --- dispatch-level: execution_configuration.customProperties must be auto-filled from the
# project's stored Global Parameters, the same way the Run TestBot dialog does before submitting
# (regression coverage for {{username}}-style variables being typed literally instead of resolving
# to a real value — confirmed live: the platform has no fallback of its own for a missing/empty
# customProperties list, it just leaves the placeholder text in the field). ---

async def test_dispatch_execute_bot_autofills_custom_properties_from_global_parameters(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://some-cloud-grid.example.com/wd/hub"}

    async def fake_list_global_parameters():
        return {"customProperties": [
            {"customPropertyId": "cp-1", "name": "username", "value": "support@automationhq.ai"},
            {"customPropertyId": "cp-2", "name": "password", "value": "secret"},
        ]}

    async def fake_execute_bot(bot_id, execution_configuration, **kwargs):
        captured["execution_configuration"] = execution_configuration
        return {"executionId": "e1"}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", fake_list_global_parameters)
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", fake_execute_bot)

    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(),  # no customProperties supplied
    }, DEFAULT_BUNDLE)

    props = captured["execution_configuration"]["customProperties"]
    assert {"customPropertyId": "cp-1", "name": "username", "value": "support@automationhq.ai"} in props
    assert {"customPropertyId": "cp-2", "name": "password", "value": "secret"} in props


async def test_dispatch_execute_bot_does_not_clobber_caller_supplied_custom_properties(monkeypatch):
    captured = {}

    async def fake_get_grid(grid_id):
        return {"gridId": grid_id, "url": "https://some-cloud-grid.example.com/wd/hub"}

    async def fake_list_global_parameters():
        raise AssertionError("must not fetch global parameters when the caller already supplied an override")

    async def fake_execute_bot(bot_id, execution_configuration, **kwargs):
        captured["execution_configuration"] = execution_configuration
        return {"executionId": "e1"}

    monkeypatch.setattr(DEFAULT_BUNDLE.config, "get_grid", fake_get_grid)
    monkeypatch.setattr(DEFAULT_BUNDLE.config, "list_global_parameters", fake_list_global_parameters)
    monkeypatch.setattr(DEFAULT_BUNDLE.executor, "execute_bot", fake_execute_bot)

    override = [{"customPropertyId": "cp-9", "name": "username", "value": "override@example.com"}]
    await _dispatch("execute_bot", {
        "bot_id": "b1",
        "execution_configuration": _valid_exec_config(customProperties=override),
    }, DEFAULT_BUNDLE)

    assert captured["execution_configuration"]["customProperties"] == override
