import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

from mcp.server.lowlevel.server import request_ctx

from src import mcp_server
from src.config.ahq_services import REPO_ROOT
from src.mcp_server import TOOLS, list_tools
from src.tool_groups import GROUPS, PROFILES, resolve_tool_names

ALL_TOOL_NAMES = {t.name for t in TOOLS}


def _grouped() -> list[str]:
    return [name for names in GROUPS.values() for name in names]


def test_groups_are_an_exact_partition_of_the_tool_surface():
    """A tool in no group can never appear in any profile, and nothing else would notice.

    Both directions matter. An ungrouped tool is invisible to every profile that isn't `full`;
    a grouped name that no longer exists is a rename nobody propagated, which silently shrinks
    whichever profile referenced it.
    """
    grouped = _grouped()
    assert set(grouped) - ALL_TOOL_NAMES == set(), "grouped names that are not real tools"
    assert ALL_TOOL_NAMES - set(grouped) == set(), "tools missing from every group"

    duplicates = {n for n in grouped if grouped.count(n) > 1}
    assert not duplicates, f"tools in more than one group: {duplicates}"


def test_profiles_only_reference_real_tools():
    for profile, names in PROFILES.items():
        assert names <= ALL_TOOL_NAMES, f"{profile} references unknown tools: {names - ALL_TOOL_NAMES}"


# --- the constraint that would break a client mid-workflow -------------------------------

def _skill_tools() -> dict[str, set[str]]:
    """Tool names each SKILL.md declares, read from its own frontmatter.

    Parsed here rather than via prompts.parse_skill_md because that parser deliberately skips
    indented lines — it only needs the scalars. The `tools:` list is exactly what this test is
    about, so it reads the raw file. Entries are namespaced (mcp__testbots-mcp-server__list_bots);
    only the bare tool name is meaningful to us.
    """
    out: dict[str, set[str]] = {}
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1] if text.startswith("---") else ""
        out[skill_md.parent.name] = {
            m.group(1) for m in re.finditer(r"^\s*-\s*mcp__[\w-]+__(\w+)\s*$", frontmatter, re.M)
        }
    return out


def _skill_body_tools() -> dict[str, set[str]]:
    """Tool names each SKILL.md's PROSE names, whether or not the frontmatter declares them.

    The frontmatter is a hand-maintained list and drifts from what the body actually instructs;
    that drift is how a workflow ends up depending on a tool nobody checked. Anything the body
    names in backticks that is also a real tool counts, because a body that discusses a tool the
    client cannot see leaves the model stuck at exactly that sentence — a mention is a dependency
    whether or not the author remembered to declare it.
    """
    out: dict[str, set[str]] = {}
    for skill_md in sorted((REPO_ROOT / "skills").glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        body = text.split("---", 2)[2] if text.startswith("---") else text
        out[skill_md.parent.name] = set(re.findall(r"`(\w+)`", body)) & ALL_TOOL_NAMES
    return out


def test_every_skill_works_under_the_core_profile():
    """`core` is DEFINED as "keeps the bundled skills working" — this is that definition.

    A skill whose tools are hidden doesn't fail cleanly; it runs until it reaches the missing
    tool and then improvises. Half a workflow is worse than a long tool list, so a skill that
    grows a dependency outside core has to either bring the tool into core or move out of it —
    and finds out here rather than in front of a customer.
    """
    core = PROFILES["core"]
    skills = _skill_tools()
    assert skills, "no skills parsed — the frontmatter format changed"

    missing = {name: sorted(tools - core) for name, tools in skills.items() if tools - core}
    assert not missing, f"skills referencing tools hidden by `core`: {missing}"


def test_core_covers_every_tool_the_skill_bodies_name():
    """The frontmatter is the declared contract; the body is what the model actually reads.

    Checking only the declaration let `list_branches` and `commit_branch` sit outside `core`
    while the test stayed green — both generation skills instruct the model to offer the real
    branches before creating a script, so under `?profile=core` it was told to do something it
    had no tool for, and fell back to the protected `main` the rule exists to avoid.
    """
    core = PROFILES["core"]
    bodies = _skill_body_tools()
    assert bodies, "no skill bodies parsed — the file format changed"

    missing = {name: sorted(tools - core) for name, tools in bodies.items() if tools - core}
    assert not missing, f"skill prose names tools hidden by `core`: {missing}"


def test_skill_frontmatter_references_real_tools():
    """Catches a skill pointing at a tool that was renamed or never existed."""
    for skill, tools in _skill_tools().items():
        assert tools <= ALL_TOOL_NAMES, f"{skill} references unknown tools: {sorted(tools - ALL_TOOL_NAMES)}"


# --- resolution ---------------------------------------------------------------------------

def test_no_spec_means_no_filtering():
    for spec in (None, "", "   "):
        assert resolve_tool_names(spec) is None


def test_explicit_full_wins_over_anything_else_in_the_spec():
    assert resolve_tool_names("core,full") is None


def test_unknown_profile_falls_back_to_everything_not_nothing():
    """A typo must cost the reduction, never the tools.

    Resolving `?profile=cor` to an empty list would present as a server with no capabilities —
    which reads as broken rather than as misconfigured, and is exactly the sort of thing that
    only surfaces during a demo.
    """
    assert resolve_tool_names("cor") is None
    assert resolve_tool_names("nonsense,alsononsense") is None


def test_core_is_a_strict_subset():
    core = resolve_tool_names("core")
    assert core is not None
    assert core < ALL_TOOL_NAMES


def test_groups_compose_with_profiles():
    core = resolve_tool_names("core")
    combined = resolve_tool_names("core,api")
    assert combined == core | set(GROUPS["api"])


def test_bare_group_names_resolve_on_their_own():
    assert resolve_tool_names("healing") == set(GROUPS["healing"]) | set(GROUPS["context"])


def test_orientation_is_never_filtered_out():
    """get_context is how the model learns which org/project it is pointed at."""
    for spec in ("api", "healing", "core", "vault,mocks"):
        assert "get_context" in resolve_tool_names(spec), spec


def test_spec_parsing_tolerates_whitespace_and_case():
    assert resolve_tool_names(" Core , API ") == resolve_tool_names("core,api")


# --- how a profile actually reaches list_tools ---------------------------------------------

@contextmanager
def hosted_request(query: dict[str, str], headers: dict[str, str] | None = None):
    """Install a fake Starlette request the way the HTTP transport does.

    list_tools reads server.request_context, a ContextVar only the transport sets, so without
    this the hosted branch is unreachable from tests — and the query parameter is the ONLY lever
    connector clients (Claude, ChatGPT, Copilot Studio) have, since they configure a URL and
    nothing else.

    A context manager rather than a fixture on purpose: an async test body runs in its own
    copied Context, so a token taken in fixture setup cannot be reset in fixture teardown
    ("created in a different Context"). Entering and exiting inside the test keeps both halves
    on the same side of that boundary.
    """
    token = request_ctx.set(SimpleNamespace(request=SimpleNamespace(
        query_params=query, headers=headers or {},
    )))
    try:
        yield
    finally:
        request_ctx.reset(token)


async def test_hosted_query_parameter_selects_the_profile():
    with hosted_request({"profile": "core"}):
        assert len(await list_tools()) == len(PROFILES["core"])


async def test_hosted_header_is_accepted_too():
    with hosted_request({}, {"x-ahq-tool-profile": "healing"}):
        names = {t.name for t in await list_tools()}
    assert names == set(GROUPS["healing"]) | set(GROUPS["context"])


async def test_hosted_default_is_still_every_tool():
    with hosted_request({}):
        assert len(await list_tools()) == len(TOOLS)


async def test_hosted_request_ignores_the_stdio_env_setting(monkeypatch):
    """A multi-tenant container's own env must never narrow what one client's URL asked for.

    The hosted process serves every tenant; AHQ_MCP_TOOL_PROFILE is a single-tenant stdio knob.
    Reading it on the hosted path would let one deployment-wide value silently truncate the tool
    list for clients that never asked for a subset.
    """
    monkeypatch.setattr(mcp_server.settings, "ahq_mcp_tool_profile", "healing")
    with hosted_request({}):
        assert len(await list_tools()) == len(TOOLS)


# --- the payoff -----------------------------------------------------------------------------

def test_core_meaningfully_reduces_the_wire_payload():
    """The whole point of the profile. If the reduction isn't large, it isn't worth the branch.

    Measured on the real wire format: `exclude_none` plus compact separators. A naive
    model_dump() keeps every unset field and overstates the payload by ~29%, which would flatter
    this number on both sides of the comparison.
    """
    core = resolve_tool_names("core")
    kept = [t for t in TOOLS if t.name in core]
    size = lambda ts: len(json.dumps(  # noqa: E731
        [t.model_dump(exclude_none=True, by_alias=True) for t in ts], separators=(",", ":")
    ))

    full_chars, core_chars = size(TOOLS), size(kept)
    assert core_chars < full_chars * 0.6, (
        f"core is {core_chars} chars vs full {full_chars} — under a 40% saving it is not "
        f"earning the extra code path."
    )
