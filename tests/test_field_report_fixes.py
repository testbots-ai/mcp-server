"""
Fixes from the second field report (2026-07-13/14): friendly TypeValuePair forms,
add_test_steps/update_test_script, list paging, ResponseObj slimming, add_locators IDs.
"""
import pytest
from pydantic import ValidationError

from src.clients.test_mgmt_client import (
    TestMgmtClient, TYPE_VALUE_PAIR_CODES, _normalize_step_parameters,
)
from src.mcp_server import _slim_response_obj
from src.schema.asset_kinds import AddTestStepsArgs, UpdateTestScriptArgs


# --- friendly TypeValuePair forms ---

def test_friendly_forms_translate_to_type_codes():
    steps = [{"templateId": "template-id-3", "parameters": [
        {"key": "text", "value": {"configuration": "baseUrl"}},
        {"key": "password", "value": {"vault": "loginPassword"}},
        {"key": "expected", "value": {"literal": "Dashboard"}},
    ]}]
    _normalize_step_parameters(steps)
    values = [p["value"] for p in steps[0]["parameters"]]
    assert values[0] == {"type": 2, "value": "baseUrl"}
    assert values[1] == {"type": 7, "value": "loginPassword"}
    assert values[2] == {"type": 0, "value": "Dashboard"}
    assert all("TypeValuePair" in p["paramClass"] for p in steps[0]["parameters"])


def test_raw_shapes_and_locator_refs_pass_through_untouched():
    steps = [{"parameters": [
        {"key": "text", "value": {"type": 0, "value": "x"}, "paramClass": "already.Set"},
        {"key": "ui-locator", "value": {"locatorId": "abc"}},
    ]}]
    _normalize_step_parameters(steps)
    assert steps[0]["parameters"][0]["value"] == {"type": 0, "value": "x"}
    assert steps[0]["parameters"][0]["paramClass"] == "already.Set"
    assert steps[0]["parameters"][1]["value"] == {"locatorId": "abc"}
    # The value is untouched, but the editor needs the class to show the element at all.
    assert steps[0]["parameters"][1]["paramClass"] == "ai.automationhq.commons.entities.assets.UILocator"


def test_full_code_table_documented():
    assert TYPE_VALUE_PAIR_CODES == {
        "literal": 0, "data_column": 1, "configuration": 2, "variable": 3,
        "parameter": 5, "faker": 6, "vault": 7,
    }


# --- add_test_steps / update_test_script ---

async def test_add_test_steps_appends_and_renumbers(monkeypatch):
    client = TestMgmtClient()
    puts = []

    async def fake_get_script(script_id):
        return {"testScriptId": script_id, "name": "S",
                "testSteps": [{"templateId": "t1", "sequence": 1}]}

    async def fake_put(path, json=None, **kwargs):
        puts.append((path, json))
        return {"ok": True}

    monkeypatch.setattr(client, "get_test_script", fake_get_script)
    monkeypatch.setattr(client, "put", fake_put)

    await client.add_test_steps("s1", [{"templateId": "t2"}, {"templateId": "t3"}])
    path, body = puts[0]
    assert path == "/rest/api/stories/scripts/s1"
    assert [s["templateId"] for s in body["testSteps"]] == ["t1", "t2", "t3"]
    assert [s["sequence"] for s in body["testSteps"]] == [1, 2, 3]


async def test_add_test_steps_inserts_at_position(monkeypatch):
    client = TestMgmtClient()
    puts = []

    async def fake_get_script(script_id):
        return {"testSteps": [{"templateId": "a", "sequence": 1}, {"templateId": "b", "sequence": 2}]}

    async def fake_put(path, json=None, **kwargs):
        puts.append(json)
        return {}

    monkeypatch.setattr(client, "get_test_script", fake_get_script)
    monkeypatch.setattr(client, "put", fake_put)

    await client.add_test_steps("s1", [{"templateId": "new"}], position=1)
    assert [s["templateId"] for s in puts[0]["testSteps"]] == ["a", "new", "b"]


async def test_update_test_script_is_get_merge_put(monkeypatch):
    client = TestMgmtClient()
    puts = []

    async def fake_get_script(script_id):
        return {"testScriptId": script_id, "name": "old", "status": "Ready",
                "storyId": "story-1", "testSteps": [{"templateId": "t1"}]}

    async def fake_put(path, json=None, **kwargs):
        puts.append(json)
        return {}

    monkeypatch.setattr(client, "get_test_script", fake_get_script)
    monkeypatch.setattr(client, "put", fake_put)

    await client.update_test_script("s1", name="new name")
    body = puts[0]
    assert body["name"] == "new name"
    assert body["storyId"] == "story-1"        # preserved
    assert body["testSteps"] == [{"templateId": "t1"}]  # preserved


def test_validators_for_new_tools():
    with pytest.raises(ValidationError):
        AddTestStepsArgs(script_id="s", steps=[])
    with pytest.raises(ValidationError):
        UpdateTestScriptArgs(script_id="s", changes={})
    UpdateTestScriptArgs(script_id="s", changes={"name": "x"})


# --- ResponseObj slimming ---

def _response_obj(**overrides):
    resp = {"timestamp": "2026-07-14", "message": "Test script added successfully",
            "details": None, "status": 0, "firstName": None, "lastName": None,
            "userId": None, "id": "abc-123", "email": None, "organizationId": None,
            "partnerId": None, "projectId": None, "userRole": None, "firstTimeLogin": True,
            "invited": False, "active": True, "validationErrors": [], "ssoEnabled": False,
            "token": None, "story": None, "success": False}
    resp.update(overrides)
    return resp


def test_response_obj_is_slimmed_and_success_derived():
    slim = _slim_response_obj(_response_obj())
    assert slim == {"id": "abc-123", "message": "Test script added successfully", "success": True}


def test_real_user_document_is_not_slimmed():
    user = {"firstName": "Onkar", "lastName": "Raut", "email": "x@y.z",
            "ssoEnabled": False, "message": None}
    assert _slim_response_obj(user) is user


def test_non_envelope_dicts_pass_through():
    data = {"testScriptId": "s1", "name": "Script"}
    assert _slim_response_obj(data) is data
