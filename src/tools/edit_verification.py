"""
Two halves of the same problem: an edit that succeeded is not a fix, and is not even in the next
run until it is committed.

Every script mutator returns "Test script updated successfully". Nothing in that response says
the change is unverified, and nothing says `execute_bot` runs the last **committed** version — so
a step added and never committed is absent from the next run, the run fails on the very thing the
step was meant to fix, and no message anywhere connects the two. Confirmed live: a wait step
inserted between a sign-in click and a URL check never appeared in the execution report at all
(6 step results for a 7-step script), while the edit response, a follow-up GET, and `versionCount`
all said the edit was there.

`annotate_edit` states both facts at the moment the edit lands, costing no extra call.
`uncommitted_scripts_on` answers the same question at run time, where it is specific rather than
general — it names the scripts whose current version is not in the branch's latest commit.
"""


def annotate_edit(result, branch_name: str = None):
    """
    Attach the two things a successful edit response does not say. Applied to every script
    mutator; a failed edit is returned untouched.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    branch = branch_name or result.get("branchName")
    result["verification"] = {
        "verified_by_a_run": False,
        "committed": False,
        "why": (
            "This edit is saved but NOT committed, and execute_bot runs the last COMMITTED "
            "version — so a run started now tests the previous version and the change will look "
            "as though it did nothing."
        ),
        "next": (
            f"commit_branch({branch!r}, '<message>') then execute_bot, then read the report. "
            f"Do not describe this change as fixed until a run has actually passed."
            if branch else
            "commit_branch on this script's branch, then execute_bot, then read the report. "
            "Do not describe this change as fixed until a run has actually passed."
        ),
    }
    return result


def _commit_version_ids(commit: dict) -> set:
    """
    The script versions a commit pinned. The field has appeared under more than one name across
    responses, so read all of them rather than assuming — a wrong guess here reports every script
    as uncommitted, which is worse than reporting none.
    """
    for key in ("scriptVersionIds", "testScriptVersionIds", "versionIds"):
        value = commit.get(key)
        if isinstance(value, dict):
            return {v for v in value.values() if v}
        if isinstance(value, list):
            return {v for v in value if isinstance(v, str)}
    return set()


async def uncommitted_scripts_on(clients, branch_name: str):
    """
    Scripts on `branch_name` whose current version is not in the branch's latest commit.

    Returns [] when there is nothing to report AND when the check cannot be run — deliberately
    indistinguishable, because this sits in front of execute_bot and a preflight that blocks a run
    over its own failed lookup is worse than the problem it prevents. Same discipline as
    _preflight_execution_configuration.
    """
    if not branch_name:
        return []
    try:
        scripts = await clients.test_mgmt.get_scripts_for_branch(branch_name)
        commits = await clients.test_mgmt.list_commits(branch_name, page=0, size=1)
    except Exception:
        return []

    if isinstance(commits, dict):
        commits = commits.get("content") or commits.get("commits") or []
    if not isinstance(commits, list) or not commits or not isinstance(scripts, list):
        return []

    pinned = _commit_version_ids(commits[0] if isinstance(commits[0], dict) else {})
    if not pinned:
        return []

    stale = []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        current = script.get("currentVersionId")
        if current and current not in pinned:
            stale.append({
                "testScriptId": script.get("testScriptId") or script.get("id"),
                "name": script.get("name"),
            })
    return stale


def staleness_warning(stale, branch_name: str) -> dict:
    """The advisory attached to a run whose branch carries uncommitted script edits."""
    names = ", ".join(s["name"] or s["testScriptId"] or "?" for s in stale[:5])
    more = "" if len(stale) <= 5 else f" (+{len(stale) - 5} more)"
    return {
        "uncommitted_edits_on_branch": branch_name,
        "scripts": stale,
        "warning": (
            f"{len(stale)} script{'' if len(stale) == 1 else 's'} on '{branch_name}' "
            f"{'has' if len(stale) == 1 else 'have'} edits that are NOT in the latest commit: "
            f"{names}{more}. execute_bot runs the last COMMITTED version, so this run will not "
            f"include those edits. Commit them with commit_branch first if the run is meant to "
            f"test them."
        ),
    }
