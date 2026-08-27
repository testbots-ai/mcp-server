"""
"Edited" is not "fixed", and is not even in the next run until it is committed.

Confirmed live before this existed: a wait step inserted between a sign-in click and a URL check
never appeared in the execution report at all — 6 step results for a 7-step script — while the
edit response, a follow-up GET and versionCount all said the edit was there. execute_bot runs the
last COMMITTED version, and nothing said so at the moment it mattered.
"""

import asyncio

import pytest

from src.tools.edit_verification import (
    annotate_edit,
    staleness_warning,
    uncommitted_scripts_on,
)


def run(coro):
    return asyncio.run(coro)


# --- the edit-time half --------------------------------------------------------------------

def test_a_successful_edit_says_it_is_neither_committed_nor_verified():
    result = annotate_edit({"message": "Test script updated successfully"}, "feature/login-fix")
    v = result["verification"]

    assert v["verified_by_a_run"] is False
    assert v["committed"] is False
    assert "last COMMITTED version" in v["why"]
    assert "feature/login-fix" in v["next"]
    assert result["message"] == "Test script updated successfully", "original response preserved"


def test_the_branch_falls_back_to_whatever_the_edit_reported():
    result = annotate_edit({"message": "ok", "branchName": "feature/from-response"})
    assert "feature/from-response" in result["verification"]["next"]


def test_an_edit_with_no_known_branch_still_gets_the_warning():
    assert "commit_branch" in annotate_edit({"message": "ok"})["verification"]["next"]


def test_a_failed_edit_is_left_alone():
    assert annotate_edit({"error": "Concurrent edit refused"}) == {"error": "Concurrent edit refused"}


# --- the run-time half ---------------------------------------------------------------------

class _TestMgmt:
    def __init__(self, scripts, commits, fail=False):
        self.scripts, self.commits, self.fail = scripts, commits, fail

    async def get_scripts_for_branch(self, branch_name):
        if self.fail:
            raise RuntimeError("ReadTimeout")
        return self.scripts

    async def list_commits(self, branch_name, page=0, size=20):
        if self.fail:
            raise RuntimeError("ReadTimeout")
        return self.commits


class _Clients:
    def __init__(self, test_mgmt):
        self.test_mgmt = test_mgmt


SCRIPTS = [
    {"testScriptId": "s1", "name": "Login Module", "currentVersionId": "v9"},
    {"testScriptId": "s2", "name": "Dashboard Module", "currentVersionId": "v4"},
]


def test_a_script_edited_since_the_last_commit_is_named():
    commits = {"content": [{"scriptVersionIds": ["v4"]}]}
    stale = run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main"))

    assert [s["name"] for s in stale] == ["Login Module"]

    warning = staleness_warning(stale, "main")
    assert "Login Module" in warning["warning"]
    assert "1 script on 'main' has" in warning["warning"], "pluralise on the count"


def test_pluralisation_on_more_than_one():
    # A commit pinning some OTHER version — not an empty list, which is deliberately read as
    # "could not determine" rather than "nothing is committed" (see the test below).
    commits = {"content": [{"scriptVersionIds": ["v1"]}]}
    stale = run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main"))
    assert "2 scripts on 'main' have" in staleness_warning(stale, "main")["warning"]


def test_everything_committed_reports_nothing():
    commits = {"content": [{"scriptVersionIds": ["v9", "v4"]}]}
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main")) == []


def test_the_version_id_field_is_read_under_any_of_its_names():
    for key in ("scriptVersionIds", "testScriptVersionIds", "versionIds"):
        commits = {"content": [{key: ["v9", "v4"]}]}
        assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main")) == [], key


def test_a_version_id_map_is_accepted_as_well_as_a_list():
    commits = {"content": [{"scriptVersionIds": {"s1": "v9", "s2": "v4"}}]}
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main")) == []


def test_a_check_that_cannot_run_reports_nothing_rather_than_blocking_a_run():
    """This sits in front of execute_bot. A preflight that fails its own lookup must not stop
    a run — that is worse than the problem it prevents."""
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, {}, fail=True)), "main")) == []


def test_an_unrecognised_commit_shape_reports_nothing_rather_than_everything():
    """A wrong field guess would mark every script stale — noise that trains people to ignore it."""
    commits = {"content": [{"somethingElse": ["v9"]}]}
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, commits)), "main")) == []


def test_no_branch_and_no_commits_are_both_no_ops():
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, {})), None)) == []
    assert run(uncommitted_scripts_on(_Clients(_TestMgmt(SCRIPTS, {"content": []})), "main")) == []


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
