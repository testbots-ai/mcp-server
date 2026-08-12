"""
Slice 9b: Archive Manager — generic list/restore/permanently-delete across ~10 entity types.

Route quirks under test (from the real controllers in ahq-user-management-services):
- generic entities: GET {prefix}/archived, POST {prefix}/{id}/restore, DELETE {prefix}/{id}/permanent
- locator: list is GET on the prefix ROOT, permanent delete is DELETE /{id} (no /permanent)
- recorded_script: lives in ahq-test-management-services entirely (RecordedScriptController)
- the generic ArchiveController reads the "organizationId" header, not "org-id"
"""

import httpx
import pytest
from pydantic import ValidationError

from src.clients.user_client import UserClient, ARCHIVE_ENTITY_PATHS
from src.mcp_server import _dispatch, DEFAULT_BUNDLE
from src.schema.asset_kinds import ARCHIVE_ENTITY_TYPES, ArchiveAssetArgs, ArchiveListArgs


def _client_with_fake_transport(fake_request):
    client = UserClient()
    client._client.request = fake_request
    return client


async def _async_result(value):
    return value


def test_entity_type_sets_stay_in_sync():
    # The validator's enum must cover exactly the client's route table plus recorded_script
    # (routed to TestMgmtClient by the dispatcher).
    assert ARCHIVE_ENTITY_TYPES == set(ARCHIVE_ENTITY_PATHS) | {"recorded_script"}


class TestValidators:
    def test_rejects_unknown_entity_type(self):
        with pytest.raises(ValidationError):
            ArchiveListArgs(entity_type="widget")

    def test_accepts_every_known_entity_type(self):
        for entity_type in ARCHIVE_ENTITY_TYPES:
            ArchiveAssetArgs(entity_type=entity_type, asset_id="a1")

    def test_rejects_blank_asset_id(self):
        with pytest.raises(ValidationError):
            ArchiveAssetArgs(entity_type="epic", asset_id="  ")


async def test_list_archived_generic_entity_hits_archived_suffix_with_org_header():
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["params"] = kwargs.get("params")
        return httpx.Response(200, json={"content": []}, request=httpx.Request(method, url))

    client = _client_with_fake_transport(fake_request)
    await client.list_archived("test_script", search="login")

    assert captured["url"].endswith("/api/test-scripts/archived")
    assert captured["headers"]["organizationId"] == client._credentials.org_id
    assert captured["params"]["search"] == "login"


async def test_list_archived_locator_hits_root_not_archived_suffix():
    captured = {}

    async def fake_request(method, url, **kwargs):
        captured["url"] = url
        return httpx.Response(200, json={"content": []}, request=httpx.Request(method, url))

    client = _client_with_fake_transport(fake_request)
    await client.list_archived("locator")
    assert captured["url"].endswith("/api/archived-locators")
    assert "/archived-locators/archived" not in captured["url"]


async def test_permanent_delete_generic_vs_locator_suffix():
    captured = []

    async def fake_request(method, url, **kwargs):
        captured.append((method, url))
        return httpx.Response(200, json={"success": True}, request=httpx.Request(method, url))

    client = _client_with_fake_transport(fake_request)
    await client.permanently_delete_archived("epic", "e1")
    await client.permanently_delete_archived("locator", "l1")

    assert captured[0][0] == "DELETE" and captured[0][1].endswith("/api/epics/e1/permanent")
    # LocatorArchiveController's delete is DELETE /{id} with NO /permanent suffix
    assert captured[1][0] == "DELETE" and captured[1][1].endswith("/api/archived-locators/l1")


async def test_restore_asset_dispatches_generic_to_user_client():
    sentinel = {"success": True}
    original = DEFAULT_BUNDLE.user.restore_archived
    DEFAULT_BUNDLE.user.restore_archived = lambda entity_type, asset_id: _async_result(sentinel)
    try:
        result = await _dispatch(
            "restore_asset", {"entity_type": "story", "asset_id": "s1"}, DEFAULT_BUNDLE, is_hosted=False
        )
    finally:
        DEFAULT_BUNDLE.user.restore_archived = original
    assert result == sentinel


async def test_recorded_script_archive_routes_to_test_mgmt_client_not_user_client():
    # recorded_script's archive endpoints live in ahq-test-management-services — routing it to
    # UserClient would 404 (and KeyError locally). All three tools must branch.
    sentinel = {"status": 200}
    calls = []

    def fake_user(*a, **kw):
        raise AssertionError("recorded_script must never route to UserClient")

    def track(name):
        def fake(*a, **kw):
            calls.append(name)
            return _async_result(sentinel)
        return fake

    originals = (
        DEFAULT_BUNDLE.user.list_archived,
        DEFAULT_BUNDLE.user.restore_archived,
        DEFAULT_BUNDLE.user.permanently_delete_archived,
        DEFAULT_BUNDLE.test_mgmt.list_archived_recorded_scripts,
        DEFAULT_BUNDLE.test_mgmt.restore_recorded_script,
        DEFAULT_BUNDLE.test_mgmt.permanently_delete_recorded_script,
    )
    DEFAULT_BUNDLE.user.list_archived = fake_user
    DEFAULT_BUNDLE.user.restore_archived = fake_user
    DEFAULT_BUNDLE.user.permanently_delete_archived = fake_user
    DEFAULT_BUNDLE.test_mgmt.list_archived_recorded_scripts = track("list")
    DEFAULT_BUNDLE.test_mgmt.restore_recorded_script = track("restore")
    DEFAULT_BUNDLE.test_mgmt.permanently_delete_recorded_script = track("delete")
    try:
        await _dispatch("list_archived_assets", {"entity_type": "recorded_script"}, DEFAULT_BUNDLE, is_hosted=False)
        await _dispatch("restore_asset", {"entity_type": "recorded_script", "asset_id": "r1"}, DEFAULT_BUNDLE, is_hosted=False)
        await _dispatch("permanently_delete_asset", {"entity_type": "recorded_script", "asset_id": "r1", "confirmed": True}, DEFAULT_BUNDLE, is_hosted=False)
    finally:
        (DEFAULT_BUNDLE.user.list_archived,
         DEFAULT_BUNDLE.user.restore_archived,
         DEFAULT_BUNDLE.user.permanently_delete_archived,
         DEFAULT_BUNDLE.test_mgmt.list_archived_recorded_scripts,
         DEFAULT_BUNDLE.test_mgmt.restore_recorded_script,
         DEFAULT_BUNDLE.test_mgmt.permanently_delete_recorded_script) = originals
    assert calls == ["list", "restore", "delete"]


async def test_unknown_entity_type_rejected_before_any_client_call():
    def _boom(*a, **kw):
        raise AssertionError("client should never be called for an unknown entity_type")

    original = DEFAULT_BUNDLE.user.list_archived
    DEFAULT_BUNDLE.user.list_archived = _boom
    try:
        result = await _dispatch(
            "list_archived_assets", {"entity_type": "widget"}, DEFAULT_BUNDLE, is_hosted=False
        )
    finally:
        DEFAULT_BUNDLE.user.list_archived = original
    assert "error" in result
    assert "entity_type" in result["error"]
