"""
Regressions for step-locator resolution.

The mistake being prevented: a wait step meant for "Add Custom Properties Button" was written
with the id of "Generate API Token Button" — a real locator on a different page of the same
application. The call succeeded, the step title rendered normally, and the error only appeared
as a failed run. Repairing it needed a JSON diff across 69 steps to confirm nothing else moved.
"""

import asyncio

import pytest

from src.tools.locator_validation import (
    annotate,
    locator_refs_in_steps,
    refusal_for,
    resolve_step_locators,
)


def run(coro):
    return asyncio.run(coro)


def _step(sequence, locator_id, title="Wait for visibility"):
    return {
        "sequence": sequence,
        "templateTitle": title,
        "parameters": [
            {"key": "ui-locator", "value": {"locatorId": locator_id}},
            {"key": "number", "value": {"type": 0, "value": "10"}},
        ],
    }


class _Asset:
    """Pages keyed by locator id. A missing id raises the 404 the real endpoint returns."""

    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.calls = []

    async def get_page_by_locator_id(self, website_id, locator_id):
        self.calls.append(locator_id)
        if self.error:
            raise self.error
        if locator_id not in self.pages:
            raise RuntimeError("Client error '404 Not Found'")
        return self.pages[locator_id]


class _Clients:
    def __init__(self, asset):
        self.asset = asset


ADD_PROPS = "c0da6419-1111-2222-3333-444444444444"
API_TOKEN = "68e5df70-5555-6666-7777-888888888888"

PAGES = {
    ADD_PROPS: {
        "pageName": "Global Settings",
        "pageUrl": "https://app.example.com/admin/global-settings",
        "locators": [{"locatorId": ADD_PROPS, "locatorName": "Add Custom Properties Button"}],
    },
    API_TOKEN: {
        "pageName": "API Tokens",
        "pageUrl": "https://app.example.com/admin/api-tokens",
        "locators": [{"locatorId": API_TOKEN, "locatorName": "Generate API Token Button"}],
    },
}


def test_refs_are_collected_including_nested_sub_steps():
    steps = [
        _step(1, ADD_PROPS),
        {"sequence": 2, "subTestSteps": [_step(None, API_TOKEN)]},
    ]
    refs = locator_refs_in_steps(steps)
    assert [r["locator_id"] for r in refs] == [ADD_PROPS, API_TOKEN]


def test_a_valid_but_wrong_locator_is_written_and_named():
    """
    The real #8 case. Both ids are valid and on the same website, so nothing can refuse this —
    but the response must say WHICH element was used, which is what makes the error visible.
    """
    clients = _Clients(_Asset(PAGES))
    resolution = run(resolve_step_locators(clients, "site-1", [_step(1, API_TOKEN)]))

    assert resolution["unresolved"] == []
    assert resolution["resolved"][0]["locator_name"] == "Generate API Token Button"
    assert resolution["resolved"][0]["page_name"] == "API Tokens"

    annotated = annotate({"message": "Test script updated successfully"}, resolution)
    assert annotated["resolved_locators"][0]["locator_name"] == "Generate API Token Button"
    assert "check_this" in annotated


def test_an_unresolvable_locator_is_reported_and_refused():
    clients = _Clients(_Asset(PAGES))
    ghost = "00000000-dead-beef-0000-000000000000"
    resolution = run(resolve_step_locators(clients, "site-1", [_step(4, ghost)]))

    assert [u["locator_id"] for u in resolution["unresolved"]] == [ghost]
    refusal = refusal_for(resolution["unresolved"], "site-1")
    assert "Nothing was written" in refusal["error"]
    assert ghost in refusal["error"]
    assert "step 4" in refusal["error"]


def test_each_distinct_locator_is_looked_up_once():
    asset = _Asset(PAGES)
    clients = _Clients(asset)
    steps = [_step(1, ADD_PROPS), _step(2, ADD_PROPS), _step(3, API_TOKEN)]
    resolution = run(resolve_step_locators(clients, "site-1", steps))

    assert sorted(asset.calls) == sorted({ADD_PROPS, API_TOKEN})
    assert len(resolution["resolved"]) == 3, "every step still reported, one lookup per element"


def test_a_lookup_that_cannot_run_never_becomes_the_failure():
    """Fail-open: a transport error marks the check unverified, it does not block the edit."""
    clients = _Clients(_Asset(PAGES, error=RuntimeError("ReadTimeout")))
    resolution = run(resolve_step_locators(clients, "site-1", [_step(1, ADD_PROPS)]))

    assert resolution["unresolved"] == []
    assert resolution["unverified"] is True
    annotated = annotate({"message": "ok"}, resolution)
    assert "not verified" in annotated["locator_check"]


def test_no_website_id_means_unverified_not_refused():
    clients = _Clients(_Asset(PAGES))
    resolution = run(resolve_step_locators(clients, None, [_step(1, ADD_PROPS)]))
    assert resolution["unresolved"] == []
    assert resolution["unverified"] is True


def test_steps_without_locators_do_nothing():
    clients = _Clients(_Asset(PAGES))
    steps = [{"sequence": 1, "parameters": [{"key": "number", "value": {"type": 0, "value": "5"}}]}]
    resolution = run(resolve_step_locators(clients, "site-1", steps))
    assert resolution == {"resolved": [], "unresolved": [], "unverified": False}


def test_annotate_leaves_an_error_response_alone():
    resolution = {"resolved": [{"locator_id": ADD_PROPS}], "unresolved": [], "unverified": False}
    assert annotate({"error": "boom"}, resolution) == {"error": "boom"}


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
