import asyncio

from src.clients.base_client import BaseAhqClient
from src.config.ahq_services import TEST_MGMT_SVC

# The platform's step titles rarely use the word a caller reaches for first. Searching
# "Navigate" — the obvious term for opening a URL — matches only "Navigate back" and
# "Navigate forward"; the step that actually opens one is titled "Open Web Browser and go to
# page", so the natural query misses it and an agent concludes the action doesn't exist. Search
# is a plain substring match server-side, so the expansion has to happen here.
_TEMPLATE_TITLE_ALIASES = {
    "navigate": ("Open Web Browser", "go to page"),
    "go to": ("Open Web Browser", "go to page"),
    "goto": ("Open Web Browser", "go to page"),
    "open url": ("Open Web Browser", "go to page"),
    "open page": ("Open Web Browser", "go to page"),
    "visit": ("Open Web Browser", "go to page"),
    "launch": ("Open Web Browser",),
    "browse": ("Open Web Browser",),
    "type": ("Enter",),
    "fill": ("Enter",),
    "input": ("Enter",),
    "set value": ("Enter",),
    "assert": ("Verify",),
    "check": ("Verify",),
    "expect": ("Verify",),
    "should": ("Verify",),
    "validate": ("Verify",),
    "dropdown": ("Select",),
    "choose": ("Select",),
    "hover": ("Mouse",),
    "screenshot": ("Capture",),
    "sleep": ("Wait",),
    "pause": ("Wait",),
}

# A script listing is for finding the script you want; its steps are what get_test_script is
# for. Returning every step tree inline made one match cost ~6KB and a 100-script project
# unreadable.
_SCRIPT_SUMMARY_FIELDS = (
    "testScriptId", "name", "status", "type", "storyId", "websiteId", "pageId",
    "currentBranchName", "updatedDate",
)

# TypeValuePair.type codes (from typeAwareDisplay() in the backend) — the full table, so nobody
# has to mine backend source again. The friendly single-key forms below are translated by
# _normalize_step_parameters before any script write.
TYPE_VALUE_PAIR_CODES = {
    "literal": 0,        # {"literal": "text"}           -> value is the literal string
    "data_column": 1,    # {"data_column": "colName"}    -> data-driven column reference
    "configuration": 2,  # {"configuration": "baseUrl"}  -> global/config parameter name
    "variable": 3,       # {"variable": "varName"}       -> runtime variable
    "parameter": 5,      # {"parameter": "paramName"}    -> parameter reference
    "faker": 6,          # {"faker": "Email"}            -> fake-data generator name
    "vault": 7,          # {"vault": "secretName"}       -> vault secret name
}

_TVP_CLASS = "ai.automationhq.commons.entities.assets.TypeValuePair"

# List endpoints reject size<=0 in at least some deployments ("Page size must not be less than
# one" — hit live on AHQ2.0 Master while the same -1/-1 worked on another org). 0/500 works
# everywhere; results beyond 500 are out of scope for a discovery list.
_LIST_ALL = {"offset": 0, "size": 500}

# Field names vary across report shapes, so try each and fall back to leaving the order alone —
# a wrong sort would be worse than the server's, since the caller cannot see that it happened.
_RUN_TIME_KEYS = ("executionDate", "startTime", "createdAt", "createdTime", "executionStartTime")


def _newest_first(runs: list) -> list:
    key = next((k for k in _RUN_TIME_KEYS
                if any(isinstance(r, dict) and r.get(k) for r in runs)), None)
    if not key:
        return runs
    return sorted(runs, key=lambda r: str(r.get(key) or ""), reverse=True)


def _normalize_step_parameters(steps):
    """
    Accept friendly TypeValuePair forms in step parameters and translate them to the raw
    {"type": <code>, "value": ...} shape, e.g. {"configuration": "baseUrl"} -> type 2,
    {"vault": "password"} -> type 7. Raw shapes pass through untouched.
    """
    for step in steps or []:
        for param in step.get("parameters") or []:
            value = param.get("value")
            if isinstance(value, dict) and len(value) == 1:
                key = next(iter(value))
                if key in TYPE_VALUE_PAIR_CODES:
                    param["value"] = {"type": TYPE_VALUE_PAIR_CODES[key], "value": value[key]}
                    param.setdefault("paramClass", _TVP_CLASS)
    return steps


class TestMgmtClient(BaseAhqClient):
    def __init__(self, credentials=None, http_client=None):
        super().__init__(TEST_MGMT_SVC, credentials, http_client)

    # --- Test Scripts ---
    @staticmethod
    def _slim_script_summary(script: dict) -> dict:
        slim = {k: script[k] for k in _SCRIPT_SUMMARY_FIELDS if script.get(k) is not None}
        steps = script.get("testSteps")
        if isinstance(steps, list):
            slim["stepCount"] = len(steps)
        return slim

    async def list_test_scripts(self, name: str = None) -> list:
        # `name` is a plain case-insensitive substring match and works as expected. If a script
        # you just created is missing here, check the CREDENTIALS this client is using before
        # suspecting the filter or the branch. Results are scoped to org_id + project_id together,
        # and two configurations can differ in BOTH: this repo's .env token and the installed
        # plugin's resolve to different organizations as well as different projects, so each is
        # blind to the other's assets and neither is wrong. A mismatched org/project PAIR (say,
        # one config's org header with the other's project id) returns a sparse result rather than
        # an error, which reads exactly like data going missing.
        params = dict(_LIST_ALL)
        if name:
            params["name"] = name
        result = await self.get("/rest/api/stories/scripts/list", params=params)
        result = result.get("content", result) if isinstance(result, dict) else result
        if isinstance(result, list):
            return [self._slim_script_summary(s) if isinstance(s, dict) else s for s in result]
        return result

    async def get_test_script(self, script_id: str, branch_name: str = None) -> dict:
        # Without branchName the platform returns the script as it is on the caller's checked-out
        # branch, not the branch an edit is for. An edit built from that copy and saved onto its
        # own branch replaced the branch's steps with another branch's - every earlier change to
        # the script was lost while each edit reported success.
        params = {"branchName": branch_name} if branch_name else None
        return await self.get(f"/rest/api/stories/scripts/{script_id}", params=params)

    async def create_test_script(
        self,
        name: str,
        steps: list = None,
        page_id: str = None,
        website_id: str = None,
        story_id: str = None,
        status: str = "Not Started",
        script_type: str = "WEB",
        branch_name: str = "main",
        repair_comment: str = None,
    ) -> dict:
        # websiteId is a separate field from pageId on TestScript — the UI's "Application" column
        # and its filtering key off websiteId, NOT pageId. A script with pageId but no websiteId
        # was invisible in the Table View despite existing and being correctly branch-scoped
        # (confirmed live, 2026-07-08). Likewise storyId: a script with no story attached was
        # excluded from the Table View's default "Test Scripts" listing even though the user-guide
        # documents that view as a flat list of ALL scripts — confirmed by diffing against every
        # other script in the same project, which all had a storyId.
        # status/type are plain String fields with no server-side default — sending them as JSON
        # null (i.e. omitting them) trips the UI's editor validation ("Expected string, received
        # null") when the script is opened. "Not Started"/"WEB" match real scripts in this project.
        # branch_name is ALWAYS sent explicitly — omitting currentBranchName makes the server fall
        # back to this API token's ambient "checked out branch" ProjectState for the project, which
        # is NOT reliably "main" (confirmed live: two scripts created back-to-back with no explicit
        # branch landed on different branches — one on "main", the next on "Test" — with no payload
        # difference between the two calls). Never rely on that default; always be explicit.
        # An unrecognised branch name is not rejected — the server forks a NEW branch with that
        # name and puts the script there, so a typo ("mian") silently creates a branch nobody
        # looks at and the script is missing from every view the user checks. Confirmed live:
        # probe values like "definitely-not-a-real-branch-xyz" became real branches in the
        # project. Creating a branch should be a deliberate create_branch call, never a
        # side effect of misspelling one here.
        if branch_name:
            branches = await self.list_branches()
            # Never let this check itself become the failure: an unexpected response shape means
            # "cannot verify", which must fall through to the create rather than block it.
            known = (
                {b.get("branchName") for b in branches if isinstance(b, dict)}
                if isinstance(branches, list)
                else set()
            )
            if known and branch_name not in known:
                raise ValueError(
                    f"Branch '{branch_name}' does not exist in this project, and creating a "
                    f"script against an unknown branch silently forks a new branch with that "
                    f"name rather than failing. Existing branches: "
                    f"{sorted(n for n in known if n)}. Call create_branch first if a new branch "
                    f"is genuinely what you want."
                )
        payload = {
            "name": name,
            "testSteps": _normalize_step_parameters(steps or []),
            "status": status,
            "type": script_type,
            "currentBranchName": branch_name,
        }
        if page_id:
            payload["pageId"] = page_id
        if website_id:
            payload["websiteId"] = website_id
        if story_id:
            payload["storyId"] = story_id
        if repair_comment:
            payload["repairComment"] = repair_comment
        return await self.post("/rest/api/stories/scripts", json=payload)

    async def _put_script(self, script_id: str, document: dict, branch_name: str = None) -> dict:
        """
        The write half of every GET-merge-PUT script edit, plus the branch discipline both halves
        need.

        A GET returns `currentBranchName` set to THIS REQUEST'S ambient checked-out branch, not
        the branch the script really lives on (see get_test_script's own tool description). PUTting
        that value straight back is what makes an edit land somewhere nobody asked for: a step
        added to a script the user created on `main` went to whatever branch the token happened to
        be pointed at, the next run tested the old version, and nothing in either response said so.
        Passing branch_name pins the destination; the landing branch is always echoed back so the
        caller can see where the edit actually went even when it doesn't.
        """
        if branch_name:
            document["currentBranchName"] = branch_name
        landed = document.get("currentBranchName")
        result = await self.put(f"/rest/api/stories/scripts/{script_id}", json=document)
        if isinstance(result, dict) and landed:
            result["branchName"] = landed
        return result

    async def update_test_script(self, script_id: str, branch_name: str = None,
                                 expected_version: int = None, **changes) -> dict:
        # PUT /rest/api/stories/scripts/{id} is a full-document update — same GET-merge-PUT
        # discipline as update_common_function so a partial body can never wipe fields.
        # NOTE: direct edits to a script on a PROTECTED branch (often "main") 403 with
        # "Create a working branch and use a Pull Request" — that error is the platform's
        # version-control policy, not a client bug.
        current, version, refusal = await self._steps_for_edit(script_id, expected_version, branch_name)
        if refusal:
            return refusal
        if "testSteps" in changes:
            changes["testSteps"] = _normalize_step_parameters(changes["testSteps"])
        current.update(changes)
        return await self._put_script_if_unchanged(script_id, current, branch_name, version)

    async def add_test_steps(
        self, script_id: str, steps: list, position: int = None, branch_name: str = None,
        expected_version: int = None,
    ) -> dict:
        """
        Append (or insert at `position`, 0-based) steps to an existing script — the single-call
        replacement for the fetch-spec/read-controller/hand-build-PUT detour. Sequences are
        renumbered across the whole script.
        """
        current, version, refusal = await self._steps_for_edit(script_id, expected_version, branch_name)
        if refusal:
            return refusal
        existing = current.get("testSteps") or []
        pos = len(existing) if position is None else max(0, min(position, len(existing)))
        merged = existing[:pos] + _normalize_step_parameters(list(steps)) + existing[pos:]
        for i, step in enumerate(merged, start=1):
            step["sequence"] = i
        current["testSteps"] = merged
        return await self._put_script_if_unchanged(script_id, current, branch_name, version)

    async def _steps_for_edit(self, script_id: str, expected_version, branch_name: str = None):
        """
        Read a script for an edit, refusing if it has moved since the caller last looked.
        `versionCount` is TestScript's monotonic counter across all branches, so an unchanged
        value means nobody else has written since.

        This exists because every script edit here is GET-merge-PUT with no server-side
        optimistic locking: two writers who both read version N each PUT a full document built
        from it, and the second silently erases the first's steps while both calls report
        success. That is not hypothetical — two agents editing one script concurrently produced
        a duplicated block and two steps spliced into the middle of a form-entry sequence, and
        nothing in either response indicated a collision.
        """
        current = await (self.get_test_script(script_id, branch_name) if branch_name else self.get_test_script(script_id))
        if not isinstance(current, dict):
            return None, None, {"error": f"Script {script_id} response was not a document: {current}"}
        version = current.get("versionCount")
        if expected_version is not None and version != expected_version:
            return None, None, {"error": (
                f"Concurrent edit refused: script {script_id} is at versionCount {version}, but "
                f"expected_version was {expected_version}. Someone (or another agent) changed "
                f"this script after you read it. Re-read it with get_test_script, rebuild the "
                f"change against the current steps, and retry."
            )}
        return current, version, None

    async def _put_script_if_unchanged(self, script_id: str, document: dict,
                                       branch_name: str, seen_version):
        """
        Last check before the write: re-read the script and bail if `versionCount` moved while
        this edit was being assembled. Closes the read-modify-write window itself, so a caller
        who never passes expected_version still cannot silently overwrite a concurrent edit.
        """
        latest = await (self.get_test_script(script_id, branch_name) if branch_name else self.get_test_script(script_id))
        if isinstance(latest, dict) and seen_version is not None:
            current_version = latest.get("versionCount")
            if current_version != seen_version:
                return {"error": (
                    f"Concurrent edit refused: script {script_id} changed from versionCount "
                    f"{seen_version} to {current_version} while this edit was being prepared — "
                    f"another writer is active on it right now. Nothing was written. Re-read the "
                    f"script and retry."
                )}
        return await self._put_script(script_id, document, branch_name)

    async def delete_test_steps(self, script_id: str, sequences: list = None,
                                step_ids: list = None, branch_name: str = None,
                                expected_version: int = None) -> dict:
        """
        Remove specific steps by 1-based `sequence` and/or `testStepId`, renumbering what remains.

        Without this, deleting one step from a 75-step script meant fetching the whole script,
        rebuilding the entire corrected testSteps array by hand, and PUTting all 75 back — an
        all-or-nothing rewrite where a slip damages steps nobody meant to touch.
        """
        if not sequences and not step_ids:
            return {"error": "Pass sequences and/or step_ids — nothing to delete."}
        current, version, refusal = await self._steps_for_edit(script_id, expected_version, branch_name)
        if refusal:
            return refusal

        existing = current.get("testSteps") or []
        drop_seq = set(sequences or [])
        drop_ids = set(step_ids or [])
        kept, removed = [], []
        for step in existing:
            if step.get("sequence") in drop_seq or step.get("testStepId") in drop_ids:
                removed.append({"sequence": step.get("sequence"),
                                "testStepId": step.get("testStepId"),
                                "testStepTitle": step.get("testStepTitle")})
            else:
                kept.append(step)

        matched_seq = {r["sequence"] for r in removed}
        matched_ids = {r["testStepId"] for r in removed}
        unmatched_seq = sorted(drop_seq - matched_seq)
        unmatched_ids = sorted(drop_ids - matched_ids)
        if unmatched_seq or unmatched_ids:
            # Deleting the wrong steps is unrecoverable, so a request that does not describe the
            # script the caller thinks it does must not be half-applied.
            return {"error": (
                f"Nothing was deleted — these did not match any step in script {script_id}: "
                f"sequences={unmatched_seq or []}, step_ids={unmatched_ids or []}. The script has "
                f"{len(existing)} steps (sequences 1 to {len(existing)}). Re-read it and retry."
            )}

        for index, step in enumerate(kept, start=1):
            step["sequence"] = index
        current["testSteps"] = kept
        result = await self._put_script_if_unchanged(script_id, current, branch_name, version)
        if isinstance(result, dict) and "error" not in result:
            result["removed_steps"] = removed
            result["remaining_step_count"] = len(kept)
        return result

    async def reorder_test_steps(self, script_id: str, order: list,
                                 branch_name: str = None, expected_version: int = None) -> dict:
        """
        Reorder steps by listing their CURRENT 1-based sequences in the order they should end up.
        `order` must be a permutation of every existing sequence — a partial list is rejected
        rather than guessed at, since the missing steps' fate would be ambiguous.
        """
        current, version, refusal = await self._steps_for_edit(script_id, expected_version, branch_name)
        if refusal:
            return refusal

        existing = current.get("testSteps") or []
        expected = list(range(1, len(existing) + 1))
        if sorted(order) != expected:
            return {"error": (
                f"order must be a permutation of every current sequence {expected} — got {order}. "
                f"Nothing was written. List all {len(existing)} sequences in their new order."
            )}

        by_sequence = {step.get("sequence"): step for step in existing}
        reordered = [by_sequence[seq] for seq in order]
        for index, step in enumerate(reordered, start=1):
            step["sequence"] = index
        current["testSteps"] = reordered
        return await self._put_script_if_unchanged(script_id, current, branch_name, version)

    # --- Epics ---
    async def list_epics(self) -> list:
        result = await self.get("/rest/api/epics/list", params=dict(_LIST_ALL))
        return result.get("content", result) if isinstance(result, dict) else result

    async def get_epic(self, epic_id: str) -> dict:
        return await self.get(f"/rest/api/epics/{epic_id}")

    async def create_epic(self, name: str) -> dict:
        return await self.post("/rest/api/epics", json={"name": name})

    # --- Stories ---
    async def list_stories(self, epic_id: str) -> list:
        return await self.get(f"/rest/api/epics/{epic_id}/stories/list")

    async def create_story(self, epic_id: str, name: str) -> dict:
        return await self.post(f"/rest/api/epics/{epic_id}/stories", json={"name": name})

    # --- Test Bots ---
    async def list_bots(self, name: str = None) -> list:
        params = dict(_LIST_ALL)
        if name:
            params["name"] = name
        result = await self.get("/rest/api/testbots/list", params=params)
        return result.get("content", result) if isinstance(result, dict) else result

    async def get_bot(self, bot_id: str) -> dict:
        return await self.get(f"/rest/api/testbots/{bot_id}")

    # The three values the platform actually recognises. Anything else is stored verbatim and
    # then quietly ignored: resolveBrevoTemplateId returns 0, the `overrideId > 0` guard skips
    # the override, and the report goes out on the default template. The WITH_FAILURES branch
    # also never fires, so failedScriptsHtml is not injected and the mail loses its failure
    # detail — with nothing anywhere reporting a problem. Seen live as "SUMMARY_CHARTS_TOTAL",
    # a constant invented from the dropdown's label because no validated path existed.
    REPORT_TEMPLATE_TYPES = (
        "TEST_EXECUTION_REPORT_SUMMARY",
        "TEST_EXECUTION_REPORT_WITH_FAILURES",
        "TEST_EXECUTION_REPORT_EXECUTIVE",
    )

    async def get_bot_report_config(self, bot_id: str) -> dict:
        # Returns server-side defaults rather than 404ing when a bot has never been configured.
        return await self.get(f"/rest/api/testbots/{bot_id}/report-config")

    async def save_bot_report_config(self, bot_id: str, recipients: list = None,
                                     notify_on_pass: bool = None, notify_on_fail: bool = None,
                                     attach_pdf: bool = None, template_type: str = None,
                                     schedule_on_completion: bool = None) -> dict:
        """Change some of a bot's report settings, leaving the rest as they are.

        GET-merge-PUT, and not optional. saveReportConfig calls setNotifyOnPass/setNotifyOnFail/
        setAttachPdf/setScheduleOnCompletion unconditionally, and those are Java `boolean`
        PRIMITIVES — an absent JSON field deserializes to false, so a partial PUT silently
        switches notifications off rather than leaving them alone. Only templateType is
        null-guarded server-side. Same destructive-PUT class as update_common_function.
        """
        if template_type is not None and template_type not in self.REPORT_TEMPLATE_TYPES:
            return {"error": f"template_type must be one of {', '.join(self.REPORT_TEMPLATE_TYPES)} "
                             f"— got {template_type!r}. The server stores an unrecognised value "
                             f"without complaint and then ignores it at send time, so the report "
                             f"goes out on the default template."}

        current = await self.get_bot_report_config(bot_id)
        if not isinstance(current, dict):
            current = {}
        body = {
            "testBotId": bot_id,
            "recipients": current.get("recipients") or [],
            "notifyOnPass": current.get("notifyOnPass", True),
            "notifyOnFail": current.get("notifyOnFail", True),
            "attachPdf": current.get("attachPdf", False),
            "templateType": current.get("templateType") or self.REPORT_TEMPLATE_TYPES[1],
            "scheduleOnCompletion": current.get("scheduleOnCompletion", True),
        }
        for key, value in (("recipients", recipients), ("notifyOnPass", notify_on_pass),
                           ("notifyOnFail", notify_on_fail), ("attachPdf", attach_pdf),
                           ("templateType", template_type),
                           ("scheduleOnCompletion", schedule_on_completion)):
            if value is not None:
                body[key] = value
        return await self.put(f"/rest/api/testbots/{bot_id}/report-config", json=body)

    async def create_test_bot(self, name: str, test_suites: list, description: str = "",
                              bot_type: dict = None, folder_id: str = None,
                              profile_id: str = None, number_of_retries: int = 0) -> dict:
        """
        POST /rest/api/testbots (TestBotController.addTestBot). A TestBot carries NO
        browser/grid/environment config — that arrives at trigger time as an
        ExecutionConfiguration on the execute call. Scripts attach only through Test Suites
        (testSuites min 1, each {testSuiteId, name}); the server rejects duplicate bot names,
        validates every referenced suite exists, and defaults botType to REGRESSION_TEST.
        """
        body = {
            "name": name,
            "description": description,
            "testSuites": test_suites,
            "excludeFromAnalytics": True,
            "numberOfRetries": number_of_retries,
        }
        if bot_type:
            body["botType"] = bot_type
        if folder_id:
            body["testBotFolderId"] = folder_id
        if profile_id:
            body["profileId"] = profile_id
        return await self.post("/rest/api/testbots", json=body)

    async def list_bot_types(self) -> list:
        return await self.get("/rest/api/testbots/getAllTestBotTypes")

    async def list_recent_reports(self, bot_id: str = None, limit: int = 10):
        # TestReportController (test-management). The old list_recent_runs hit
        # GET /background-jobs/execution-jobs, which never existed (404 forever).
        if bot_id:
            # Trimmed here rather than by the server: this path takes no paging params, so a
            # caller asking for the last run got every run the bot has ever had and `limit` was
            # silently a lie. Newest first, so a trim keeps the ones that were asked for.
            result = await self.get(f"/rest/api/testreports/{bot_id}")
            return _newest_first(result)[:limit] if isinstance(result, list) else result
        result = await self.get("/rest/api/testreports/bots/list",
                                params={"offset": 0, "size": limit, "sortBy": "name"})
        return result.get("content", result) if isinstance(result, dict) else result

    # --- Test Suites (Test Sets) ---
    async def list_suites(self) -> list:
        result = await self.get("/rest/api/suites/list", params=dict(_LIST_ALL))
        return result.get("content", result) if isinstance(result, dict) else result

    async def get_suite(self, suite_id: str) -> dict:
        return await self.get(f"/rest/api/suites/{suite_id}")

    async def create_suite(self, name: str, scripts: list = None) -> dict:
        # Scripts are EMBEDDED in the TestSuite document (testScripts:
        # List<TestScriptForTestSuiteView>) — there is no separate attach endpoint.
        return await self.post("/rest/api/suites", json={"name": name, "testScripts": scripts or []})

    async def add_scripts_to_suite(self, suite_id: str, script_ids: list) -> dict:
        # There is NO POST /suites/{id}/scripts endpoint (the path this method used to call) —
        # TestSuiteController only has POST (create) and PUT /{id} (full update). Scripts live
        # embedded in the suite document, so this is a GET-merge-PUT, same pattern as
        # update_common_function: fetch the suite, append the new scripts (resolving each
        # script's name/sequence), and PUT the whole document back.
        suite = await self.get_suite(suite_id)
        existing = suite.get("testScripts") or []
        existing_ids = {s.get("testScriptId") for s in existing}
        seq = max((s.get("sequence", 0) for s in existing), default=0)
        for script_id in script_ids:
            if script_id in existing_ids:
                continue
            script = await self.get_test_script(script_id)
            seq += 1
            existing.append({
                "testScriptId": script_id,
                "name": script.get("name", ""),
                "status": script.get("status"),
                "selected": True,
                "sequence": seq,
            })
        suite["testScripts"] = existing
        return await self.put(f"/rest/api/suites/{suite_id}", json=suite)

    async def remove_scripts_from_suite(self, suite_id: str, script_ids: list) -> dict:
        # Same GET-modify-PUT as the add path, for the same reason: scripts are embedded in the
        # suite document and there is no per-script detach endpoint. Sequences are renumbered so
        # the remaining scripts stay 1..n with no gaps, matching what create/add produce.
        suite = await self.get_suite(suite_id)
        removing = set(script_ids)
        kept = [s for s in (suite.get("testScripts") or []) if s.get("testScriptId") not in removing]
        for index, script in enumerate(kept, start=1):
            script["sequence"] = index
        suite["testScripts"] = kept
        return await self.put(f"/rest/api/suites/{suite_id}", json=suite)

    # --- Recorded Scripts ---
    # RecordedScriptController reads @RequestHeader("organizationId") — NOT the "org-id" header
    # every OTHER controller in this same service uses (TestScriptController, EpicController, ...).
    # A recorded-script call sent with only the default headers 400s with a missing-header error.
    def _recorded_headers(self) -> dict:
        return {"organizationId": self._credentials.org_id}

    async def list_recorded_scripts(self, name: str = None, branch_name: str = None) -> dict:
        params = {"offset": 0, "size": 100}
        if name:
            params["name"] = name
        if branch_name:
            params["branchName"] = branch_name
        return await self.get(
            "/rest/api/recorded-scripts", params=params, extra_headers=self._recorded_headers()
        )

    async def get_recorded_script(self, recorded_script_id: str) -> dict:
        return await self.get(
            f"/rest/api/recorded-scripts/{recorded_script_id}",
            extra_headers=self._recorded_headers(),
        )

    async def promote_recorded_script(
        self,
        recorded_script_id: str,
        story_id: str,
        name: str = None,
        website_id: str = None,
        status: str = None,
        description: str = None,
        tags: list = None,
        reusable: bool = None,
        steps: list = None,
        keep_step_ids: list = None,
        branch_name: str = "main",
    ) -> dict:
        # storyId travels as a QUERY PARAM, not in the body — the body is the optional
        # PromoteRecordedScript overrides object. The server requires storyId for a first-time
        # promotion (repeat promotions update the already-linked TestScript and ignore it).
        # branch_name defaults to "main" explicitly for the same reason create_test_script always
        # sends currentBranchName: a null/blank branch makes the server auto-commit against this
        # API token's ambient "checked out branch" ProjectState, which is not reliably "main".
        body = {"currentBranchName": branch_name}
        if name:
            body["name"] = name
        if website_id:
            body["websiteId"] = website_id
        if status:
            body["status"] = status
        if description:
            body["description"] = description
        if tags:
            body["tags"] = tags
        if reusable is not None:
            body["reusable"] = reusable
        if steps:
            body["steps"] = steps
        if keep_step_ids:
            body["keepStepIds"] = keep_step_ids
        return await self.post(
            f"/rest/api/recorded-scripts/{recorded_script_id}/promote",
            json=body,
            params={"storyId": story_id},
            extra_headers=self._recorded_headers(),
        )

    # Recorded-script archive endpoints live HERE (RecordedScriptController), not in
    # ahq-user-management-services like every other entity's Archive Manager endpoints —
    # the dispatcher routes entity_type="recorded_script" to these.
    async def list_archived_recorded_scripts(self, name: str = None, page: int = 0, size: int = 50) -> dict:
        # This controller pages with "offset", unlike the generic ArchiveController's "page".
        params = {"offset": page, "size": size}
        if name:
            params["name"] = name
        return await self.get(
            "/rest/api/recorded-scripts/archived", params=params, extra_headers=self._recorded_headers()
        )

    async def restore_recorded_script(self, recorded_script_id: str) -> dict:
        return await self.post(
            f"/rest/api/recorded-scripts/{recorded_script_id}/restore",
            extra_headers=self._recorded_headers(),
        )

    async def permanently_delete_recorded_script(self, recorded_script_id: str) -> dict:
        return await self.delete(
            f"/rest/api/recorded-scripts/{recorded_script_id}/permanent",
            extra_headers=self._recorded_headers(),
        )

    # --- Version Control: branches & commits ---
    # ProjectBranchController, base /rest/api/projects/{projectId}/branches, standard org-id
    # header. Branch names travel as QUERY params (never path segments) so names with slashes
    # (feature/login) work.
    def _branches_base(self) -> str:
        return f"/rest/api/projects/{self._credentials.project_id}/branches"

    async def list_branches(self, query: str = None) -> list:
        params = {"q": query} if query else None
        return await self.get(self._branches_base(), params=params)

    async def delete_branch(self, branch_name: str) -> dict:
        # Scripts living on the deleted branch are moved back to main, and a ProjectState still
        # pointing at it is reset to main — so this removes the branch, not the work on it.
        # The default and protected branches are refused server-side with a 400.
        await self.delete(self._branches_base(), params={"branchName": branch_name})
        return {"deleted": branch_name, "note": "Scripts that were on this branch are back on main."}

    async def get_scripts_for_branch(self, branch_name: str) -> list:
        # THE correct way to answer "which scripts are on branch X" — real membership lives in
        # per-script branch records, NOT in TestScript.currentBranchName (filtering on that field
        # undercounted 1 vs the UI's 7 in live testing, 2026-07-11).
        return await self.get(f"{self._branches_base()}/scripts", params={"branchName": branch_name})

    async def create_branch(
        self,
        branch_name: str,
        from_branch: str = "main",
        strategy: str = None,
        confirmed: bool = False,
        script_ids: list = None,
        is_protected: bool = False,
    ) -> dict:
        # Two-phase server-side: the first call runs a preflight conflict check and may return
        # status NEEDS_CONFIRMATION instead of creating anything — the caller must resend with
        # confirmed=true (surfaced to the tool caller, never auto-retried here).
        body = {
            "branchName": branch_name,
            "fromBranch": from_branch,
            "confirmed": confirmed,
            # Lombok `boolean isProtected` -> Jackson property "protected"; send both spellings
            # (same trap as CreateRoleRequest.isDefault).
            "protected": is_protected,
            "isProtected": is_protected,
        }
        if strategy:
            body["strategy"] = strategy
        if script_ids:
            body["scriptIds"] = script_ids
        return await self.post(self._branches_base(), json=body)

    async def commit_branch(self, branch_name: str, message: str, tag: str = None) -> dict:
        body = {"message": message}
        if tag:
            body["tag"] = tag
        return await self.post(
            f"{self._branches_base()}/commit", params={"branchName": branch_name}, json=body
        )

    async def list_commits(self, branch_name: str, page: int = 0, size: int = 20) -> dict:
        return await self.get(
            f"{self._branches_base()}/commits",
            params={"branchName": branch_name, "page": page, "size": size},
        )

    # --- Version Control: pull requests ---
    # PullRequestController, base /rest/api/projects/{projectId}/pull-requests. The lifecycle
    # endpoints (approve/merge/close/rebase/ready-for-review) take NO request body at all —
    # state lives server-side; do not invent one.
    def _prs_base(self) -> str:
        return f"/rest/api/projects/{self._credentials.project_id}/pull-requests"

    async def create_pull_request(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = None,
        reviewer_ids: list = None,
        script_ids: list = None,
        delete_source_branch_after_merge: bool = False,
    ) -> dict:
        body = {
            "sourceBranch": source_branch,
            "targetBranch": target_branch,
            "title": title,
            "deleteSourceBranchAfterMerge": delete_source_branch_after_merge,
        }
        if description:
            body["description"] = description
        if reviewer_ids:
            body["reviewerIds"] = reviewer_ids
        if script_ids:
            body["scriptIds"] = script_ids
        return await self.post(self._prs_base(), json=body)

    async def list_pull_requests(self, status: list = None, query: str = None, page: int = 0, size: int = 20) -> dict:
        params = {"page": page, "size": size}
        if status:
            params["status"] = status
        if query:
            params["q"] = query
        return await self.get(self._prs_base(), params=params)

    async def get_pull_request(self, pr_id: str) -> dict:
        return await self.get(f"{self._prs_base()}/{pr_id}")

    async def get_pull_request_diff(self, pr_id: str, script_id: str = None) -> dict:
        params = {"scriptId": script_id} if script_id else None
        return await self.get(f"{self._prs_base()}/{pr_id}/diff", params=params)

    async def approve_pull_request(self, pr_id: str) -> dict:
        return await self.post(f"{self._prs_base()}/{pr_id}/approve")

    async def request_pr_changes(self, pr_id: str, comment: str = None) -> dict:
        body = {"comment": comment} if comment else None
        return await self.post(f"{self._prs_base()}/{pr_id}/request-changes", json=body)

    async def merge_pull_request(self, pr_id: str) -> dict:
        return await self.post(f"{self._prs_base()}/{pr_id}/merge")

    async def close_pull_request(self, pr_id: str) -> dict:
        # Closing does NOT delete the source branch.
        return await self.post(f"{self._prs_base()}/{pr_id}/close")

    # --- Project Roles ---
    # ProjectRoleController, base /rest/api/projects/{projectId}/roles — standard org-id header,
    # projectId in the PATH (also still sent as the usual header; harmless).
    def _roles_base(self) -> str:
        return f"/rest/api/projects/{self._credentials.project_id}/roles"

    async def list_project_roles(self) -> list:
        return await self.get(self._roles_base())

    async def create_project_role(self, role_name: str, permissions: list, is_default: bool = False) -> dict:
        # CreateRoleRequest declares `private boolean isDefault` with Lombok @Data — the generated
        # getter is isDefault(), so Jackson's property name is "default", not "isDefault". Send
        # both spellings; Spring Boot ignores the unknown one (FAIL_ON_UNKNOWN_PROPERTIES off).
        return await self.post(
            self._roles_base(),
            json={"roleName": role_name, "permissions": permissions, "default": is_default, "isDefault": is_default},
        )

    async def update_project_role_permissions(self, role_id: str, permissions: list) -> dict:
        # Role name is immutable server-side — the update endpoint only reads permissions, which
        # is why no name parameter exists on this method or its tool at all.
        return await self.put(f"{self._roles_base()}/{role_id}", json={"permissions": permissions})

    async def delete_project_role(self, role_id: str) -> dict:
        # System roles cannot be deleted (server-enforced); only custom roles.
        return await self.delete(f"{self._roles_base()}/{role_id}")

    async def assign_project_role(self, role_id: str, user_id: str) -> dict:
        return await self.post(f"{self._roles_base()}/{role_id}/users", json={"userId": user_id})

    async def list_project_members(self) -> list:
        return await self.get(f"{self._roles_base()}/members")

    # --- Step Templates ---
    # A TestStep's `templateId` must reference one of these — there is no static/hardcodable
    # list of action types, since templates include per-org "Common Functions" as well as
    # platform built-ins. Always resolve a real templateId before writing a step.
    @staticmethod
    def _slim_template(template: dict) -> dict:
        """Project a step template down to the fields a caller can actually act on.

        Template listing/search runs once per action while a script is being written, and the
        raw record is mostly fields nothing downstream reads: full createdBy/updatedBy user
        objects, create/update timestamps, and templateCategory/projectId/organizationId, which
        are null on every built-in. Dropping them cuts a typical search response by ~90% with no
        loss of usable information; get_template still returns the untouched record.
        """
        # Absent keys are dropped rather than emitted as null: a null carries no more meaning
        # than the missing key and still costs tokens in every listing.
        slim = {
            key: template[key]
            for key in ("templateId", "templateTitle", "description", "type")
            if template.get(key) is not None
        }
        # Params are NOT derivable from templateTitle's {{placeholders}} alone — the table-row
        # templates accept an "action-selector" that never appears in the title.
        if template.get("params"):
            slim["params"] = [
                {
                    "name": p.get("name"),
                    "allowed": p.get("allowed"),
                    "required": bool(p.get("required")),
                }
                for p in template["params"]
            ]
        for flag in ("ifConditional", "hasSubTestSteps", "encrypted"):
            if template.get(flag):
                slim[flag] = True
        return slim

    @classmethod
    def _slim_templates(cls, result):
        if isinstance(result, list):
            return [cls._slim_template(t) if isinstance(t, dict) else t for t in result]
        return result

    async def list_templates(self, offset: int = 0) -> list:
        result = await self.get(
            f"/rest/api/templates/{self._credentials.project_id}", params={"offset": offset}
        )
        result = result.get("content", result) if isinstance(result, dict) else result
        return self._slim_templates(result)

    async def search_templates(self, title: str) -> list:
        # The project-scoped /rest/api/templates/{projectId}/search only returns this org's own
        # saved custom templates (often empty). Built-in action templates (Click, Navigate, ...)
        # live org/project-agnostic and only surface through the ROOT-level /search endpoint —
        # confirmed live: {projectId}/search returned [] for every built-in title, while the root
        # endpoint returned real templateIds (e.g. "Navigate" -> 21 results including
        # templateId "template-id-178"). TemplatesController.getSearchedTemplate() (root) merges
        # global built-ins with this org's Common Functions, so it's a strict superset.
        queries = [title]
        lowered = title.lower()
        for trigger, expansions in _TEMPLATE_TITLE_ALIASES.items():
            if trigger in lowered:
                queries.extend(e for e in expansions if e not in queries)

        responses = await asyncio.gather(
            *(self.get("/rest/api/templates/search", params={"title": q}) for q in queries),
            return_exceptions=True,
        )
        # Literal-query hits stay first: an alias is a safety net for a miss, never a reranking
        # of a query that already worked.
        merged, seen = [], set()
        for response in responses:
            if not isinstance(response, list):
                continue  # an alias query failing must not fail the whole search
            for template in response:
                template_id = template.get("templateId") if isinstance(template, dict) else None
                if template_id is None or template_id in seen:
                    continue
                seen.add(template_id)
                merged.append(template)
        return self._slim_templates(merged)

    async def delete_test_script(self, script_id: str, confirmed: bool = False) -> dict:
        """Delete a script, pausing for confirmation when other assets still reference it.

        The endpoint runs its own preflight: while the script belongs to a Test Set or a TestBot
        it answers **202 with the list of them and deletes nothing**, and only a repeat call with
        `force=true` goes through (detaching it on the way). A 202 here is a question, not a
        success — it is easy to read the 2xx as "deleted" and move on believing the script is
        gone, so it is reshaped into an explicit NEEDS_CONFIRMATION result for the caller to put
        to the user, mirroring create_branch's two-phase contract.
        """
        result = await self.delete(
            f"/rest/api/stories/scripts/{script_id}",
            params={"force": "true"} if confirmed else None,
        )
        if isinstance(result, dict) and result.get("status") == 202:
            return {
                "status": "NEEDS_CONFIRMATION",
                "testScriptId": script_id,
                "message": result.get("message"),
                "next_step": (
                    "NOTHING has been deleted. Show the associations above to the user and call "
                    "delete_test_script again with confirmed=true only if they agree — that "
                    "detaches the script from every Test Set and TestBot listed."
                ),
            }
        return result

    async def get_template(self, template_id: str) -> dict:
        return await self.get(f"/rest/api/templates/{template_id}")

    # --- Scheduler ---
    # The REAL scheduler backing both the "Scheduler Admin" UI page and each TestBot's own
    # clock-icon dialog — test-management-services' /rest/api/schedulers. This is NOT the same
    # system as background-v2-services' /background-jobs/execution-jobs/schedule-* endpoints
    # (what schedule_bot_recurring/schedule_bot_once used before this): those write to a
    # completely different, UI-invisible mechanism that doesn't reliably fire at all (confirmed
    # live 2026-07-15 — a "successful" create there never actually ran, and the job vanished from
    # its own status lookup). Body shape confirmed against automationhq-frontend-v2's
    # SchedulerSchema/TSchedulerCreateSchema and its callSchedulerCreateApi.
    _SCHEDULER_RESOURCE_TYPE_TEST_BOT = 1

    def _scheduler_body(self, bot_id: str, name: str, emails: list, recurring_rule: str,
                        execution_configuration: dict) -> dict:
        return {
            "name": name,
            "emails": emails or [],
            "recurringRule": recurring_rule,
            "executionConfiguration": execution_configuration,
            "resourceId": bot_id,
            "resourceType": self._SCHEDULER_RESOURCE_TYPE_TEST_BOT,
            "organizationId": self._credentials.org_id,
            "projectId": self._credentials.project_id,
        }

    async def create_scheduler(self, bot_id: str, name: str, emails: list, recurring_rule: str,
                               execution_configuration: dict) -> dict:
        """
        POST /rest/api/schedulers — recurring schedule for a TestBot. recurringRule is a required
        cron expression; this endpoint has no confirmed one-time-run mode (see
        convert_text_to_cron for a human-language -> cron helper, and AhqCronExpression in the
        frontend for what a valid expression looks like).
        """
        body = self._scheduler_body(bot_id, name, emails, recurring_rule, execution_configuration)
        return await self.post("/rest/api/schedulers", json=body)

    async def update_scheduler(self, scheduler_id: str, bot_id: str = None, name: str = None,
                               emails: list = None, recurring_rule: str = None,
                               execution_configuration: dict = None) -> dict:
        """
        PUT /rest/api/schedulers/{id} — full-document replace, not a patch: callSchedulerUpdateApi
        takes the identical TSchedulerCreateSchema payload as create, and AddScheduler.tsx's edit
        mode always fetches the existing record first and resubmits the whole thing (the same
        destructive-PUT shape already hit once in this codebase — see update_common_function).
        GETs the current scheduler and merges in only the fields the caller actually wants
        changed, so updating just the cron expression can't silently wipe emails or the execution
        config.
        """
        current = await self.get_scheduler(scheduler_id)
        body = self._scheduler_body(
            bot_id if bot_id is not None else current.get("resourceId"),
            name if name is not None else current.get("name"),
            emails if emails is not None else current.get("emails"),
            recurring_rule if recurring_rule is not None else current.get("recurringRule"),
            execution_configuration if execution_configuration is not None else current.get("executionConfiguration"),
        )
        return await self.put(f"/rest/api/schedulers/{scheduler_id}", json=body)

    async def list_schedulers(self, bot_id: str = None, offset: int = 0, size: int = 100) -> dict:
        # Matches ListSchedulers.tsx's own filter shape exactly — the TestBot scheduler drawer
        # (the "Schedulers" panel in the UI) filters strictly by resourceId == this bot's id, so
        # a schedule created against the wrong bot_id would succeed but never appear there. Pass
        # bot_id to reproduce that exact view and confirm what a given bot actually has.
        body = {"offset": offset, "size": size, "sortBy": "createdDate", "orderBy": "desc", "resourceType": 1}
        if bot_id:
            body["resourceId"] = bot_id
        return await self.post("/rest/api/schedulers/listByFilter", json=body)

    async def get_scheduler(self, scheduler_id: str) -> dict:
        return await self.get(f"/rest/api/schedulers/{scheduler_id}")

    async def delete_scheduler(self, scheduler_id: str) -> dict:
        return await self.delete(f"/rest/api/schedulers/{scheduler_id}")

    async def toggle_scheduler(self, scheduler_id: str) -> dict:
        # Enable/disable without deleting — no request body (matches callSchedulerToggleApi).
        return await self.patch(f"/rest/api/schedulers/{scheduler_id}/toggle")

    async def list_scheduler_recipient_emails(self) -> list:
        # Previously-used recipient emails, for suggesting values rather than guessing one.
        result = await self.get("/rest/api/schedulers/emails")
        return result if isinstance(result, list) else result.get("content", result)

    async def convert_text_to_cron(self, text: str) -> dict:
        """Human-language -> cron expression, e.g. 'every day at 9am' -> '0 9 * * *'."""
        return await self.post("/rest/api/schedulers/convert-to-cron", json={"text": text, "locale": "en"})
