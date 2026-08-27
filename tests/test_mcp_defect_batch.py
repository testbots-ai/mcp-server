"""
Regressions for the defect batch found while repairing a UTAP sanity suite.

Each test here pins a failure that reported SUCCESS (or a plausible-looking result) at the call
site and only surfaced much later — the class this repo keeps getting bitten by.
"""

import asyncio
import inspect

import pytest

from src.clients.config_client import _annotate_dead_grid_status
from src.mcp_server import _slim_response_obj
from src.tools import scoped_execution
from src.tools.response_paging import paginate_script, summarize_report


def run(coro):
    return asyncio.run(coro)


# --- execute_script_only called its own clients with arguments they do not accept -------------

class _RecordingTestMgmt:
    def __init__(self):
        self.calls = []

    async def get_test_script(self, script_id):
        return {"name": "Login Module", "status": "Ready", "versionCount": 3}

    async def create_suite(self, name, scripts=None):
        self.calls.append(("create_suite", name, scripts))
        return {"id": "suite-1", "message": "created"}

    async def create_test_bot(self, name, test_suites, description="", bot_type=None,
                              folder_id=None, profile_id=None, number_of_retries=0):
        self.calls.append(("create_test_bot", name, test_suites, bot_type))
        return {"id": "bot-1", "message": "created"}


class _RecordingExecutor:
    async def execute_bot(self, bot_id, execution_configuration, name=None,
                          profile_id=None, partial_execution=False):
        return {"id": "job-1", "jobId": "job-1"}


class _Clients:
    def __init__(self):
        self.test_mgmt = _RecordingTestMgmt()
        self.executor = _RecordingExecutor()


def test_execute_script_only_matches_the_client_signatures_it_calls():
    """
    Every call failed before any script logic ran with "TestMgmtClient.create_suite() got an
    unexpected keyword argument 'description'". create_test_bot was wrong in the same way and
    would have failed the instant create_suite was fixed: it was handed `suite_ids=` (no such
    parameter), no `test_suites` (required), and bot_type as the bare string "CUSTOM" where the
    server expects a {type, value} pair or nothing.
    """
    clients = _Clients()
    result = run(scoped_execution.execute_script_only(
        clients, "script-1", {"gridId": "g", "baseUrl": "env-1"}, confirm_script_id="script-1"
    ))

    assert "error" not in result, result
    kinds = [c[0] for c in clients.test_mgmt.calls]
    assert kinds == ["create_suite", "create_test_bot"]

    _, _, scripts = clients.test_mgmt.calls[0]
    assert scripts == [{"testScriptId": "script-1", "name": "Login Module",
                        "status": "Ready", "selected": True, "sequence": 1}]

    _, bot_name, test_suites, bot_type = clients.test_mgmt.calls[1]
    assert len(test_suites) == 1
    assert test_suites[0]["testSuiteId"] == "suite-1"
    assert test_suites[0]["name"] == clients.test_mgmt.calls[0][1], "bot must carry the suite it just created"
    assert bot_name.startswith("[SCOPED] Login Module")
    assert bot_type is None, "botType must not be sent as a bare string"


def test_scoped_execution_signatures_bind_against_the_real_clients():
    """
    A cheap guard against the same class recurring: the arguments these helpers pass must still
    be accepted by the real client methods, not just by the fakes above.
    """
    from src.clients.test_mgmt_client import TestMgmtClient

    suite_params = inspect.signature(TestMgmtClient.create_suite).parameters
    assert "description" not in suite_params

    bot_params = inspect.signature(TestMgmtClient.create_test_bot).parameters
    assert "suite_ids" not in bot_params
    assert "test_suites" in bot_params


# --- the response slimmer ate the fields the clients had just added ---------------------------

def test_slimmer_keeps_client_added_fields_on_a_response_envelope():
    """
    execute_bot attaches jobId and a next_step explaining that `id` is a background JOB id, not
    an executionId. The old keep-list dropped both, so the tool returned exactly
    {id, message, success} and the caller polled the wrong id.
    """
    envelope = {
        "id": "job-1", "message": "Execution enqueued", "ssoEnabled": False,
        "firstName": None, "lastName": None, "token": None, "story": None,
        "status": 200, "success": False, "validationErrors": [],
        "jobId": "job-1", "next_step": "poll get_job_status(jobId)",
    }
    slim = _slim_response_obj(envelope)

    assert slim["jobId"] == "job-1"
    assert slim["next_step"] == "poll get_job_status(jobId)"
    assert slim["success"] is True, "success must be derived, not trusted"
    assert "ssoEnabled" not in slim and "token" not in slim


def test_slimmer_leaves_non_envelope_responses_alone():
    plain = {"testScriptId": "s1", "testSteps": [], "name": "x"}
    assert _slim_response_obj(plain) == plain


# --- grid status is not a live signal ---------------------------------------------------------

def test_dead_grid_status_is_relabelled_and_the_raw_value_is_preserved():
    grids = [{"name": "AHQ Local Agent", "status": "OFFLINE", "lastSeen": None}]
    out = _annotate_dead_grid_status(grids)
    assert out[0]["reportedStatus"] == "OFFLINE"
    assert "not a live connectivity signal" in out[0]["status"]


def test_a_grid_with_a_real_heartbeat_is_left_untouched():
    grids = [{"name": "Selenium Hub", "status": "ONLINE", "lastSeen": "2026-08-27T10:00:00Z"}]
    assert _annotate_dead_grid_status(grids)[0]["status"] == "ONLINE"


# --- step deletion is all-or-nothing ----------------------------------------------------------

class _ScriptStore:
    def __init__(self, steps, version=1):
        self.doc = {"testScriptId": "s1", "testSteps": steps, "versionCount": version,
                    "currentBranchName": "feature/x"}
        self.puts = []

    async def get(self, *_a, **_k):
        return {**self.doc, "testSteps": [dict(s) for s in self.doc["testSteps"]]}

    async def put(self, _script_id, document, _branch=None):
        self.puts.append(document)
        return {"message": "Test script updated successfully"}


def _client_with(store):
    from src.clients.test_mgmt_client import TestMgmtClient
    client = TestMgmtClient.__new__(TestMgmtClient)
    client.get_test_script = store.get
    client._put_script = store.put
    return client


def _steps(n):
    return [{"sequence": i, "testStepId": f"t{i}", "testStepTitle": f"step {i}"}
            for i in range(1, n + 1)]


def test_delete_test_steps_removes_only_the_named_steps_and_renumbers():
    store = _ScriptStore(_steps(5))
    client = _client_with(store)
    result = run(client.delete_test_steps("s1", sequences=[2, 4]))

    assert result["remaining_step_count"] == 3
    written = store.puts[0]["testSteps"]
    assert [s["testStepId"] for s in written] == ["t1", "t3", "t5"]
    assert [s["sequence"] for s in written] == [1, 2, 3]


def test_delete_test_steps_writes_nothing_when_a_sequence_does_not_exist():
    """A partially-matching delete request describes a script the caller is not actually looking
    at — applying the half that matched is how the wrong steps get removed."""
    store = _ScriptStore(_steps(3))
    client = _client_with(store)
    result = run(client.delete_test_steps("s1", sequences=[2, 99]))

    assert "error" in result and "99" in result["error"]
    assert store.puts == []


def test_reorder_requires_a_full_permutation():
    store = _ScriptStore(_steps(4))
    client = _client_with(store)
    assert "error" in run(client.reorder_test_steps("s1", [2, 1]))
    assert store.puts == []

    result = run(client.reorder_test_steps("s1", [4, 3, 2, 1]))
    assert "error" not in result
    assert [s["testStepId"] for s in store.puts[0]["testSteps"]] == ["t4", "t3", "t2", "t1"]


# --- concurrent edits are refused rather than silently merged ----------------------------------

def test_an_edit_is_refused_when_the_script_moved_since_the_caller_read_it():
    store = _ScriptStore(_steps(3), version=7)
    client = _client_with(store)
    result = run(client.delete_test_steps("s1", sequences=[1], expected_version=5))

    assert "error" in result and "versionCount" in result["error"]
    assert store.puts == []


def test_an_edit_is_refused_when_another_writer_lands_between_read_and_write():
    """The read-modify-write window itself — the case where no expected_version was passed."""
    store = _ScriptStore(_steps(3), version=1)
    client = _client_with(store)

    reads = {"n": 0}
    original_get = store.get

    async def bumping_get(*a, **k):
        reads["n"] += 1
        if reads["n"] > 1:
            store.doc["versionCount"] = 2  # somebody else wrote while we were building the edit
        return await original_get(*a, **k)

    client.get_test_script = bumping_get
    result = run(client.delete_test_steps("s1", sequences=[1]))

    assert "error" in result and "another writer is active" in result["error"]
    assert store.puts == []


# --- large responses can be asked for in pieces ------------------------------------------------

def test_script_summary_and_range_views():
    script = {"name": "big", "testSteps": _steps(80)}

    summary = paginate_script(script, summary=True)
    assert summary["stepView"]["totalSteps"] == 80
    assert set(summary["testSteps"][0]) == {"sequence", "testStepId", "title"}

    ranged = paginate_script(script, steps_from=10, steps_to=12)
    assert [s["sequence"] for s in ranged["testSteps"]] == [10, 11, 12]
    assert ranged["stepView"]["totalSteps"] == 80

    assert paginate_script(script) is script, "no paging args must not alter the document"


def test_report_summary_surfaces_the_first_failing_step_per_script():
    report = {
        "executionId": "e1", "status": "FAILED",
        "testSuiteResults": [{
            "name": "Sanity",
            "testScriptResults": [
                {"name": "Login", "status": "PASSED", "iterations": []},
                {"name": "Dashboard", "status": "FAILED", "iterations": [{
                    "testStepResults": [
                        {"sequence": 1, "status": "PASSED"},
                        {"sequence": 2, "status": "FAILED",
                         "testStepName": "Verify Elements count",
                         "statusMessage": "expected 201 but was 203"},
                    ]}]},
            ]}],
    }

    full = summarize_report(report)
    assert full["scriptCount"] == 2

    failed = summarize_report(report, failed_only=True)
    assert failed["scriptCount"] == 1
    assert failed["scripts"][0]["firstFailedStep"]["sequence"] == 2
    assert "203" in failed["scripts"][0]["firstFailedStep"]["statusMessage"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
