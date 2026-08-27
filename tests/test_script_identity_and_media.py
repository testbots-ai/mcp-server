"""
Two affordances that were missing while a whole session was spent guessing.

- The run's video was in the report payload the entire time and nothing pointed at it, so
  failures were debugged from step text alone.
- Nothing said which account a script signs in as, so "verified live in Chrome" was done as a
  Site Admin while the failing scripts ran as a Tester who cannot see the pages being verified.
"""

import pytest

from src.tools.response_paging import summarize_report
from src.tools.script_identity import describe_credentials


# --- video / screenshot in the report summary --------------------------------------------------

def _report(iterations, grid_info=None):
    script = {"name": "Login Module", "status": "FAILED", "iterations": iterations}
    if grid_info is not None:
        script["gridInfo"] = grid_info
    return {"executionId": "e1", "status": "FAILED",
            "testSuiteResults": [{"name": "Sanity", "testScriptResults": [script]}]}


def test_summary_carries_the_video_and_screenshot_urls():
    report = _report([{
        "videoUrl": "https://cdn.example.com/run-1.mp4",
        "screenshotUrl": "https://cdn.example.com/step-4.png",
        "testStepResults": [{"sequence": 4, "status": "FAILED", "statusMessage": "no such element"}],
    }])
    row = summarize_report(report)["scripts"][0]

    assert row["videoUrl"] == "https://cdn.example.com/run-1.mp4"
    assert row["screenshotUrl"] == "https://cdn.example.com/step-4.png"
    assert row["firstFailedStep"]["sequence"] == 4


def test_a_browserstack_session_video_on_grid_info_is_used_as_a_fallback():
    report = _report([{"testStepResults": []}], grid_info={"videoUrl": "https://bs.example.com/s.mp4"})
    assert summarize_report(report)["scripts"][0]["videoUrl"] == "https://bs.example.com/s.mp4"


def test_no_media_keys_when_the_run_recorded_none():
    row = summarize_report(_report([{"testStepResults": []}]))["scripts"][0]
    assert "videoUrl" not in row and "screenshotUrl" not in row


def test_the_summary_note_points_at_the_recording():
    assert "videoUrl" in summarize_report(_report([{"testStepResults": []}]))["note"]


# --- which account does the script sign in as? -------------------------------------------------

def _login_script(email_value, password_value):
    return {"testSteps": [
        {"sequence": 1, "templateTitle": "Enter {{text}} for the {{ui-locator}} email field",
         "parameters": [{"key": "text", "value": email_value}]},
        {"sequence": 2, "templateTitle": "Enter {{password}} for the password field",
         "parameters": [{"key": "password", "value": password_value}]},
    ]}


def test_a_literal_email_is_reported_as_the_identity():
    result = describe_credentials(
        _login_script({"type": 0, "value": "qa.tester@example.com"}, {"type": 0, "value": "hunter2"}))

    assert result["signs_in_as"] == ["qa.tester@example.com"]
    assert "why_this_matters" in result


def test_a_literal_password_is_never_printed():
    result = describe_credentials(
        _login_script({"type": 0, "value": "qa.tester@example.com"}, {"type": 0, "value": "hunter2"}))

    assert "hunter2" not in str(result), "a literal password must never reach the transcript"
    withheld = [s for s in result["sources"] if s["parameter"] == "password"]
    assert withheld and "not shown" in withheld[0]["value"]


def test_a_vault_reference_is_reported_by_name():
    result = describe_credentials(
        _login_script({"type": 0, "value": "qa.tester@example.com"},
                      {"type": 7, "value": "utap_sanity_password"}))

    assert "utap_sanity_password" in result["references"]
    vault = [s for s in result["sources"] if s["source"] == "vault"][0]
    assert vault["value"] == "utap_sanity_password"


def test_a_masked_encrypted_literal_is_treated_as_withheld_not_as_a_name():
    result = describe_credentials(
        _login_script({"type": 0, "value": "qa.tester@example.com"}, {"type": 0, "value": "********"}))
    password = [s for s in result["sources"] if s["parameter"] == "password"][0]
    assert password["value"] != "********"
    assert "not shown" in password["value"]


def test_configuration_and_variable_references_are_named():
    script = {"testSteps": [
        {"sequence": 1, "templateTitle": "Enter {{text}} for the username field",
         "parameters": [{"key": "text", "value": {"type": 2, "value": "sanity_user"}}]},
    ]}
    result = describe_credentials(script)
    assert result["references"] == ["sanity_user"]
    assert result["sources"][0]["source"] == "configuration"


def test_a_script_that_never_signs_in_reports_nothing():
    script = {"testSteps": [
        {"sequence": 1, "templateTitle": "Click {{ui-locator}}",
         "parameters": [{"key": "ui-locator", "value": {"locatorId": "abc"}}]},
        {"sequence": 2, "templateTitle": "Wait for {{number}} seconds",
         "parameters": [{"key": "number", "value": {"type": 0, "value": "5"}}]},
    ]}
    assert describe_credentials(script) == {}


def test_nested_sub_steps_are_searched():
    script = {"testSteps": [{
        "sequence": 1, "templateTitle": "Loop",
        "subTestSteps": [{
            "sequence": None, "templateTitle": "Enter {{password}} for the password field",
            "parameters": [{"key": "password", "value": {"type": 7, "value": "nested_secret"}}]}],
    }]}
    assert "nested_secret" in describe_credentials(script)["references"]


# --- video has to be REQUESTED, not just surfaced ----------------------------------------------

def test_video_recording_is_settable_and_reaches_the_payload():
    """
    Surfacing videoUrl is useless if no run ever records one. Capture is gated server-side on
    executionConfiguration.isVideoRecording(); the grid advertising videoRecording: true is NOT
    enough. Confirmed live on a Selenium Hub run whose grid advertises it: videoUrl was null on
    both the passing and the failing iteration, because the flag was never sent.
    """
    from src.schema.asset_kinds import RunExecutionConfiguration

    base = dict(baseUrl="env-1", browser="Chrome", gridId="g1",
                browserVersion="latest", osType="Linux")

    on = RunExecutionConfiguration(**base, videoRecording=True).model_dump(exclude_none=True)
    assert on["videoRecording"] is True

    # Off by default: the post-run path polls storage for the file, which costs time on every run.
    off = RunExecutionConfiguration(**base).model_dump(exclude_none=True)
    assert off["videoRecording"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
