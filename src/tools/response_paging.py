"""
Summary and range views for the two responses that routinely blow the tool-result token cap:
a full test script and a full execution report.

Both are normal-sized project data — a 60-90 step script, an 11-script run — not pathological
cases, and both were failing outright with "result exceeds maximum allowed tokens". A failed
fetch is worse than a big one: the caller learns nothing and has to reconstruct the data by
saving it to a file and grepping it back in chunks, which is a workaround for a missing
parameter, on nearly every diagnostic step.

The whole document is still available; these only add ways to ask for less of it.
"""


def _step_digest(step: dict) -> dict:
    """One line per step: enough to find the step you want, not enough to blow the cap."""
    digest = {
        "sequence": step.get("sequence"),
        "testStepId": step.get("testStepId"),
        "title": step.get("testStepTitle") or step.get("templateTitle"),
    }
    if step.get("subTestSteps"):
        digest["subStepCount"] = len(step["subTestSteps"])
    if step.get("skip"):
        digest["skip"] = True
    return digest


def paginate_script(script: dict, steps_from: int = None, steps_to: int = None,
                    summary: bool = False) -> dict:
    """
    Trim a test script's `testSteps` without touching any other field.

    `steps_from`/`steps_to` are INCLUSIVE 1-based sequence bounds, matching the numbers a step
    actually carries and the numbers delete_test_steps/reorder_test_steps take — an off-by-one
    between "the step numbered 5" and "index 5" is exactly the kind of slip that deletes the
    wrong step.
    """
    if not isinstance(script, dict):
        return script
    steps = script.get("testSteps")
    if not isinstance(steps, list):
        return script

    total = len(steps)
    if summary:
        trimmed = dict(script)
        trimmed["testSteps"] = [_step_digest(s) for s in steps if isinstance(s, dict)]
        trimmed["stepView"] = {
            "mode": "summary",
            "totalSteps": total,
            "note": ("Step titles only. Re-call get_test_script with steps_from/steps_to for the "
                     "full parameters of a specific range, or omit both for the whole script."),
        }
        return trimmed

    if steps_from is None and steps_to is None:
        return script

    low = 1 if steps_from is None else max(1, steps_from)
    high = total if steps_to is None else min(total, steps_to)
    selected = [s for s in steps
                if isinstance(s, dict) and low <= (s.get("sequence") or 0) <= high]

    trimmed = dict(script)
    trimmed["testSteps"] = selected
    trimmed["stepView"] = {
        "mode": "range",
        "from": low,
        "to": high,
        "showing": len(selected),
        "totalSteps": total,
    }
    return trimmed


def _iter_script_results(report: dict):
    """
    Walk the report's nested suite → script → iteration → step shape, yielding each script-level
    result with the suite it came from. Written defensively: the report nests differently
    depending on how many suites a bot carries, and a shape assumption here would turn a working
    summary into an empty one.
    """
    for suite in report.get("testSuiteResults") or report.get("suiteResults") or []:
        if not isinstance(suite, dict):
            continue
        scripts = suite.get("testScriptResults") or suite.get("scriptResults") or []
        for script in scripts:
            if isinstance(script, dict):
                yield suite, script


def _script_failed(script: dict) -> bool:
    status = script.get("resultStatus") or script.get("status") or script.get("executionStatus")
    return str(status).upper() not in ("PASSED", "PASS", "SUCCESS")


def focus_report(report: dict, script_id: str | None = None, failed_detail: bool = False) -> dict:
    """
    The full per-step report narrowed to the scripts that matter: one script by id, or every
    failed script. Fixing a failure needs that script end to end - every step up to the failure,
    its error and media - but not the passing scripts beside it, which on a multi-script bot are
    most of the report and push the failing one past the token cap. Suites left empty are dropped;
    every other field of the report is kept as it came.
    """
    if not isinstance(report, dict):
        return report
    suites_key = "testSuiteResults" if "testSuiteResults" in report else "suiteResults"
    kept_suites = []
    for suite in report.get(suites_key) or []:
        if not isinstance(suite, dict):
            continue
        scripts_key = "testScriptResults" if "testScriptResults" in suite else "scriptResults"
        scripts = [script for script in suite.get(scripts_key) or [] if isinstance(script, dict)
                   and (script_id is None or str(script.get("testScriptId")) == str(script_id))
                   and (not failed_detail or _script_failed(script))]
        if scripts:
            kept_suites.append({**suite, scripts_key: scripts})
    focused = {**report, suites_key: kept_suites}
    focused["view"] = f"script {script_id}" if script_id else "failed_detail"
    focused["note"] = ("Full per-step detail, narrowed to " + (f"script {script_id}" if script_id else "the failed scripts")
                       + ". Passing scripts are left out; call with summary=true to see them all."
                       if kept_suites else "No script matched. Call with summary=true to see the scripts in this run.")
    return focused


def summarize_report(report: dict, failed_only: bool = False) -> dict:
    """
    Collapse an execution report to one row per script, optionally only the ones that failed.

    Answers "which scripts failed and where" — the actual question behind almost every report
    fetch — without carrying every passing step's parameters and screenshot URLs along with it.
    """
    if not isinstance(report, dict):
        return report

    rows = []
    for suite, script in _iter_script_results(report):
        # Field order matters and was got wrong once: the live detailed-results payload uses
        # resultStatus at BOTH script and step level. Reading only status/executionStatus
        # returned null for every script and silently suppressed firstFailedStep entirely —
        # the single most useful field in the summary. Confirmed against execution fd49b18e.
        status = (script.get("resultStatus") or script.get("status")
                  or script.get("executionStatus"))
        failed = str(status).upper() not in ("PASSED", "PASS", "SUCCESS")
        if failed_only and not failed:
            continue

        row = {
            "suite": suite.get("name") or suite.get("testSuiteName"),
            "script": script.get("name") or script.get("testScriptName"),
            "testScriptId": script.get("testScriptId"),
            "status": status,
        }

        # Media links, carried into the summary deliberately. A summary whose whole purpose is
        # "why did this fail" that drops the recording of the failure sends the reader back to the
        # full report, which is what the summary exists to avoid.
        media = _media_for_script(script)
        row.update(media)

        # First failing step per script — the single most useful field in the whole report, and
        # the one the token cap was reliably hiding.
        for iteration in script.get("iterations") or []:
            if not isinstance(iteration, dict):
                continue
            if iteration.get("errorMessage"):
                row["errorMessage"] = iteration["errorMessage"]
            for step in iteration.get("stepResults") or iteration.get("testStepResults") or []:
                if not isinstance(step, dict):
                    continue
                step_status = str(step.get("resultStatus") or step.get("status") or "").upper()
                if step_status and step_status not in ("PASSED", "PASS", "SUCCESS", "SKIPPED"):
                    row["firstFailedStep"] = {
                        "sequence": step.get("sequence"),
                        "title": step.get("testStepName") or step.get("testStepTitle"),
                        "status": step.get("resultStatus") or step.get("status"),
                        "statusMessage": step.get("statusMessage"),
                    }
                    break
            if "firstFailedStep" in row:
                break
        rows.append(row)

    return {
        "executionId": report.get("executionId") or report.get("id"),
        "status": report.get("status") or report.get("executionStatus"),
        "view": "failed_only" if failed_only else "summary",
        "scriptCount": len(rows),
        "scripts": rows,
        "note": ("One row per script. videoUrl, when present, is the recording of that "
                 "script's run — watch it before theorising about a failure. For full per-step "
                 "detail call get_execution_report again with summary=false."),
    }


def _media_for_script(script: dict) -> dict:
    """
    First screenshot and video URL across a script's iterations.

    Video is recorded per grid session and lands on IterationResult.videoUrl (and on the run's
    GridInfo for BrowserStack/TestingBot). It has always been in this payload and nothing ever
    pointed at it, so failures were debugged from step text alone while the recording sat unused.
    """
    out = {}
    for iteration in script.get("iterations") or []:
        if not isinstance(iteration, dict):
            continue
        if "videoUrl" not in out and iteration.get("videoUrl"):
            out["videoUrl"] = iteration["videoUrl"]
        if "screenshotUrl" not in out and iteration.get("screenshotUrl"):
            out["screenshotUrl"] = iteration["screenshotUrl"]
        if len(out) == 2:
            break
    grid = script.get("gridInfo")
    if "videoUrl" not in out and isinstance(grid, dict) and grid.get("videoUrl"):
        out["videoUrl"] = grid["videoUrl"]
    return out
