"""
Grid records carry working credentials. They must never reach the model.

A live list_grids returned `accessKey` in plaintext and repeated it inside `url` as
`https://user:key@hub.browserstack.com/wd/hub` — real, usable BrowserStack and TestingBot
credentials, written into the conversation transcript on every call, stored with it, and for a
hosted deployment leaving the machine entirely.
"""

import pytest

from src.clients.config_client import _redact_grid_credentials, _strip_userinfo
from src.mcp_server import _LOCAL_GRID_URLS

BROWSERSTACK = "https://andreipangilinan_KrxPQO:g5HcnwsG3LGnBAckBsmA@hub.browserstack.com/wd/hub"
TESTINGBOT = "https://017b7c1ca7bc3019c6bdde1eea555b7c:cc08cac00f089b24f36e6519ea32e7fe@hub.testingbot.com/wd/hub"


def test_userinfo_is_stripped_but_the_grid_stays_identifiable():
    assert _strip_userinfo(BROWSERSTACK) == "https://hub.browserstack.com/wd/hub"
    assert _strip_userinfo(TESTINGBOT) == "https://hub.testingbot.com/wd/hub"


def test_a_url_with_no_credentials_is_untouched():
    for url in (
        "http://localhost:4455/wd/hub",
        "http://ahq-selenium-hub.ahq-qa.svc.cluster.local:443/wd/hub",
        "not-a-url",
    ):
        assert _strip_userinfo(url) == url, url


def test_local_grid_detection_still_matches_after_redaction():
    """
    _is_local_grid compares the grid url against these exact strings to decide whether a run goes
    to the developer's own machine or the cloud. If redaction ever rewrote them, a local run would
    silently be enqueued to a cloud that has no way to deliver it.
    """
    for url in _LOCAL_GRID_URLS:
        assert _strip_userinfo(url) == url
        grid = _redact_grid_credentials({"url": url, "accessKey": "no_key"})
        assert grid["url"] in _LOCAL_GRID_URLS


def test_a_real_key_is_redacted_and_the_username_is_kept():
    grid = _redact_grid_credentials({
        "name": "BrowserStack",
        "username": "andreipangilinan_KrxPQO",
        "accessKey": "g5HcnwsG3LGnBAckBsmA",
        "url": BROWSERSTACK,
    })
    assert "g5HcnwsG3LGnBAckBsmA" not in str(grid), "the key must appear nowhere in the response"
    assert grid["username"] == "andreipangilinan_KrxPQO", "identifying, and useless without the key"
    assert grid["url"] == "https://hub.browserstack.com/wd/hub"
    assert "urlCredentials" in grid


def test_placeholder_credentials_are_left_alone():
    """Masking `no_key` would be noise and would hide that the grid needs no credentials."""
    grid = _redact_grid_credentials({
        "name": "AHQ Local Agent",
        "username": "no_user",
        "accessKey": "no_key",
        "url": "http://localhost:4455/wd/hub",
    })
    assert grid["accessKey"] == "no_key"
    assert grid["username"] == "no_user"


def test_a_list_is_redacted_in_place_and_a_dict_is_returned_as_a_dict():
    grids = _redact_grid_credentials([{"accessKey": "abc123", "url": BROWSERSTACK}])
    assert isinstance(grids, list) and "abc123" not in str(grids)

    single = _redact_grid_credentials({"accessKey": "abc123", "url": BROWSERSTACK})
    assert isinstance(single, dict) and "abc123" not in str(single)


def test_non_grid_payloads_pass_through():
    assert _redact_grid_credentials(None) is None
    assert _redact_grid_credentials("oops") == "oops"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
