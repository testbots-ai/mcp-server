from pathlib import Path

from src.prompts import SKILL_PROMPTS, get_skill_prompt, list_skill_prompts, parse_skill_md

EXPECTED_SKILLS = {
    "testbots-dashboard",
    "testbots-gen-from-requirements",
    "testbots-gen-from-url",
    "testbots-heal-locators",
    "testbots-performance-bot",
    "testbots-run-bot",
    "testbots-schedule-bot",
    "testbots-test-architecture",
    "testbots-view-performance",
    "testbots-view-report",
}


def test_all_skills_parsed():
    assert set(SKILL_PROMPTS) == EXPECTED_SKILLS


def test_frontmatter_stripped_from_body():
    for name, entry in SKILL_PROMPTS.items():
        assert not entry["body"].startswith("---"), name
        assert "description:" not in entry["body"].splitlines()[0], name
        assert entry["body"], name


def test_names_and_descriptions_come_from_frontmatter():
    prompts = {p.name: p for p in list_skill_prompts()}
    assert set(prompts) == EXPECTED_SKILLS
    # Pinned against the file rather than a literal: the point is that descriptions are READ
    # from frontmatter, not hardcoded in prompts.py. A literal here fails on every copy edit,
    # which teaches people to update the assertion without reading what it was protecting.
    frontmatter = (Path("skills/testbots-run-bot/SKILL.md").read_text(encoding="utf-8")
                   .split("description:", 1)[1].splitlines()[0].strip())
    assert prompts["testbots-run-bot"].description == frontmatter
    assert all(p.description for p in prompts.values())


def test_get_prompt_returns_body_as_user_message():
    result = get_skill_prompt("testbots-run-bot")
    assert result.messages[0].role == "user"
    assert "Workflow" in result.messages[0].content.text


def test_hosted_note_only_on_gen_from_requirements():
    hosted = get_skill_prompt("testbots-gen-from-requirements", hosted=True)
    assert "extract_requirements" in hosted.messages[0].content.text
    assert "unavailable" in hosted.messages[0].content.text

    local = get_skill_prompt("testbots-gen-from-requirements", hosted=False)
    assert "unavailable" not in local.messages[0].content.text

    # gen-from-url needs no note — crawl_url IS hosted-enabled since Slice 9j
    hosted_url = get_skill_prompt("testbots-gen-from-url", hosted=True)
    assert "unavailable" not in hosted_url.messages[0].content.text


def test_unknown_prompt_raises():
    import pytest

    with pytest.raises(ValueError):
        get_skill_prompt("nope")


def test_parser_handles_missing_frontmatter():
    meta, body = parse_skill_md("just a body\nno fences")
    assert meta == {}
    assert body == "just a body\nno fences"


def test_parser_skips_indented_list_items():
    text = "---\nname: x\ntools:\n  - a\n  - b\ndescription: d\n---\nbody"
    meta, body = parse_skill_md(text)
    assert meta == {"name": "x", "description": "d"}
    assert body == "body"
