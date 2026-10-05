"""
Tool profiles: per-connection subsets of the 138-tool surface.

Every tool schema is serialized into the model's context on every request, before it reads the
user's first word — MCP has no lazy schema loading. Two costs follow. The obvious one is ~16.4k
tokens of fixed overhead per turn. The one that actually bites is selection accuracy: at 138
options a model starts pattern-matching on names, and picking `list_performance_bots` where
`list_bots` was meant returns an empty list rather than an error, so the answer is confidently
wrong instead of visibly broken.

A client that only needs the everyday loop can ask for `core` and stop paying for both.

Membership is kept here as a name -> group mapping rather than as a field on each Tool. Adding a
field would mean touching all 138 definitions in mcp_server.py and would conflict with every
branch in flight; a separate table is a one-line edit per tool and the partition test below is
what keeps it honest.
"""

from __future__ import annotations

# Exact partition of TOOLS — every tool in exactly one group, enforced by
# tests/test_tool_groups.py. A new tool that lands in no group fails that test, which is the
# point: the alternative is it silently never appearing in any profile.
GROUPS: dict[str, tuple[str, ...]] = {
    # Orientation. Injected into every profile (see resolve_tool_names) — a subset that can't
    # tell the model which project it's pointed at is worse than no subset.
    "context": (
        "get_context",
    # Which project to work in is a question only the user can answer, and 115 live users have
    # more than one — the model cannot ask if it cannot see the list.
    "list_my_projects",
    ),
    "discovery": (
        "list_websites",
        "search_websites",
        "create_website",
        "list_pages",
        "create_page",
        "get_page_by_url",
        "add_locators",
        "update_locator",
        "crawl_url",
    ),
    "healing": (
        "scan_broken_locators",
        "find_locator_usage",
        "heal_locator",
        "apply_locator_fix",
    ),
    "authoring": (
        "list_test_scripts",
        "get_test_script",
        "delete_test_script",
        "add_test_steps",
        "replace_test_step",
        "update_test_script",
        "delete_test_steps",
        "reorder_test_steps",
        "create_test_script",
        "list_step_templates",
        "search_step_templates",
        "get_step_template",
        "list_recorded_scripts",
        "get_recorded_script",
        "promote_recorded_script",
        "list_common_functions",
        "get_common_function",
        "create_common_function",
        "update_common_function",
        "extract_requirements",
        "inspect_script",
        "validate_script_locators",
        "check_script_for_credential_exposure",
    ),
    "planning": (
        "list_epics",
        "create_epic",
        "list_stories",
        "create_story",
        "list_suites",
        "create_suite",
        "add_scripts_to_suite",
        "remove_scripts_from_suite",
    ),
    "execution": (
        "list_bots",
        "create_test_bot",
        "list_bot_types",
        "list_grids",
        "list_browsers",
        "list_execution_types",
        "list_environments",
        "create_environment",
        "get_grid_capabilities",
        "execute_bot",
        "execute_script_only",
        "get_execution_status",
        "get_job_status",
        "check_local_agent_status",
        "list_local_agents",
    ),
    "scheduling": (
        "schedule_bot_recurring",
        "cancel_schedule",
        "update_schedule",
        "toggle_schedule",
        "list_schedulers",
        "list_scheduler_recipient_emails",
        "convert_text_to_cron",
    ),
    "reporting": (
        "list_recent_runs",
        "get_bot_notifications",
        "configure_bot_notifications",
        "get_execution_report",
        "get_performance_report",
    ),
    "versioning": (
        "list_branches",
        "get_scripts_for_branch",
        "delete_branch",
        "create_branch",
        "commit_branch",
        "list_commits",
        "create_pull_request",
        "list_pull_requests",
        "get_pull_request",
        "get_pull_request_diff",
        "approve_pull_request",
        "request_pr_changes",
        "merge_pull_request",
        "close_pull_request",
    ),
    "admin": (
        "list_project_roles",
        "create_project_role",
        "update_project_role_permissions",
        "delete_project_role",
        "assign_project_role",
        "list_project_members",
        "list_archived_assets",
        "restore_asset",
        "permanently_delete_asset",
    ),
    "vault": (
        "list_global_parameters",
        "search_global_parameters",
        "add_global_parameter",
        "check_global_parameter_usage",
        "flatten_and_delete_global_parameter",
        "list_config_vault_secrets",
        "get_config_vault_secret",
        "create_config_vault_secret",
        "update_config_vault_secret",
        "delete_config_vault_secret",
        "list_vault_secrets",
    ),
    "api": (
        "list_api_collections",
        "get_api_collection",
        "create_api_collection",
        "list_api_requests",
        "get_api_request",
        "create_api_request",
        "test_api_request",
        "import_curl",
        "import_postman_collection",
        "get_service_spec",
        "call_api",
    ),
    "performance": (
        "list_performance_bots",
        "get_performance_bot",
        "run_performance_bot",
        "stop_performance_bot",
        "get_performance_results",
    ),
    "contracts": (
        "list_consumers",
        "create_consumer",
        "list_providers",
        "create_provider",
        "list_contracts",
        "create_contract",
        "run_pact_tests",
    ),
    "mocks": (
        "list_mock_mappings",
        "get_mock_mapping",
        "get_mock_mapping_template",
        "create_mock_mapping",
        "delete_mock_mapping",
    ),
    "workflows": (
        "list_workflows",
        "get_workflow",
        "create_workflow",
        "test_workflow",
    ),
    "tunnel": (
        "get_tunnel_status",
        "start_tunnel",
        "stop_tunnel",
        "execute_tunnel_command",
    ),
    "utility": (
        "list_fake_data_types",
        "generate_fake_data",
        "send_email",
    ),
}

# `core` is not a taste judgment about which tools are "important" — it is defined as the set
# that keeps all 9 bundled skills working, plus the read/navigate tools a human needs around
# them. tests/test_tool_groups.py asserts the skill half of that from the skills' own declared
# `tools:` frontmatter, so a skill that grows a new dependency fails CI instead of half-running
# against a client that can't see the tool.
#
# Whole groups where the skills need most of the group anyway; explicit names where they don't.
_CORE_GROUPS = ("context", "discovery", "healing", "planning", "scheduling", "reporting")

_CORE_EXTRA = (
    # authoring — the create/read path. Deliberately omits delete_test_script and the
    # common-function writers: destructive or specialist, and no skill calls them.
    "list_test_scripts",
    "get_test_script",
    "add_test_steps",
    "update_test_script",
    "create_test_script",
    "list_step_templates",
    "search_step_templates",
    "get_step_template",
    "list_common_functions",
    "get_common_function",
    "extract_requirements",
    # execution — run a bot and watch it. Omits the local-agent probes, which are a
    # stdio-only troubleshooting path.
    "list_bots",
    "create_test_bot",
    "list_bot_types",
    "list_grids",
    "list_browsers",
    "list_execution_types",
    "list_environments",
    "create_environment",
    "get_grid_capabilities",
    "execute_bot",
    "get_execution_status",
    "get_job_status",
    # versioning — the branch-aware authoring loop only, not the PR surface. Both generation
    # skills tell the model to offer the real branches before creating a script, because `main`
    # is protected: an edit there stays an uncommitted version and execute_bot silently keeps
    # running the previous one. Without these the rule is advice the model cannot act on — it
    # needs list_branches/create_branch to offer a real choice, commit_branch to make the chosen
    # branch runnable at all, and get_scripts_for_branch to answer "did my edit reach this run".
    "list_branches",
    "create_branch",
    "commit_branch",
    "get_scripts_for_branch",
)

PROFILES: dict[str, frozenset[str]] = {
    "core": frozenset(
        [name for g in _CORE_GROUPS for name in GROUPS[g]] + list(_CORE_EXTRA)
    ),
}


def resolve_tool_names(spec: str | None) -> frozenset[str] | None:
    """
    Turn a profile spec into the set of tool names to expose. `None` means "expose everything" —
    the default, so no existing client changes behavior.

    A spec is a comma-separated list of profile names and/or group names, so `core,api` and
    `discovery,reporting` both work without predefining every combination anyone might want.

    Unknown tokens are ignored, and a spec that resolves to nothing falls back to the full list.
    That is the deliberate failure mode: a typo'd `?profile=cor` should cost a client the
    reduction, not every tool it has. An empty tool list looks to a user like the server is
    broken, and it is the kind of thing that only shows up in front of a customer.
    """
    if not spec:
        return None

    selected: set[str] = set()
    for token in spec.split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token == "full":
            return None
        if token in PROFILES:
            selected |= PROFILES[token]
        elif token in GROUPS:
            selected.update(GROUPS[token])

    if not selected:
        return None

    # Orientation is never worth omitting: it is one small tool, and without it the model has no
    # way to discover which org/project it is operating in.
    selected.update(GROUPS["context"])
    return frozenset(selected)
