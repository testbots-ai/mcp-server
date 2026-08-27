"""
Pre-flight validators for MCP tool arguments that create/schedule an AHQ asset.

These are ported directly from automationhq-frontend-v2's zod form schemas — the only real source
of truth for "required" in this platform. The ahq-data-commons Java entities carry zero Bean
Validation annotations (@NotNull/@NotBlank), so "required" has only ever been enforced by the
frontend forms plus backend controller side-effects, never the entity itself. Without this layer,
a missing required field either 500s deep in a downstream service (e.g. a built-in step template
missing templateTitle) or "succeeds" while producing an asset that's silently invisible/broken in
the UI (e.g. a test script with no story_id) — both failure modes are disconnected from the call
that caused them. Validating here, before any client/API call, turns both into one clear message.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

VALID_STATUSES = {"Not Started", "In Progress", "Ready", "To Be Repaired", "On Hold"}
BUILT_IN_TEMPLATE_PREFIX = "template-id-"
VALID_API_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
# From CommonFunctionSchema in automationhq-frontend-v2 — letters/digits/spaces/hyphens only.
COMMON_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9\s-]+$")


class _Args(BaseModel):
    # Tool args dicts always carry more keys than any one validator cares about (e.g. optional
    # page_id/branch_name on create_test_script) — extra="ignore" means a validator only ever
    # judges the fields it actually declares.
    model_config = ConfigDict(extra="ignore")


class TestStepIn(_Args):
    templateId: str
    templateTitle: str | None = None

    @model_validator(mode="after")
    def _built_in_needs_title(self):
        if self.templateId.startswith(BUILT_IN_TEMPLATE_PREFIX) and not self.templateTitle:
            raise ValueError(
                f"step with templateId={self.templateId!r} is a built-in template and requires "
                "templateTitle (copy the exact string, placeholders intact, from "
                "search_step_templates/get_step_template) — omitting it causes a 500 error"
            )
        return self


class TestScriptCreateArgs(_Args):
    name: str
    steps: list[TestStepIn]
    website_id: str
    story_id: str
    status: str = "Not Started"
    repair_comment: str | None = None

    @field_validator("name")
    @classmethod
    def _name_len(cls, v):
        if not (1 <= len(v) <= 120):
            raise ValueError("name must be 1-120 characters")
        return v

    @field_validator("steps")
    @classmethod
    def _steps_nonempty(cls, v):
        if not v:
            raise ValueError("steps must be a non-empty array")
        return v

    @field_validator("status")
    @classmethod
    def _status_enum(cls, v):
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        return v

    @model_validator(mode="after")
    def _repair_comment_required_when_to_be_repaired(self):
        if self.status == "To Be Repaired" and not self.repair_comment:
            raise ValueError("repair_comment is required when status is 'To Be Repaired'")
        return self


class AddTestStepsArgs(_Args):
    script_id: str = Field(min_length=1)
    steps: list[TestStepIn] = Field(min_length=1)
    position: int | None = Field(default=None, ge=0)


class UpdateTestScriptArgs(_Args):
    script_id: str = Field(min_length=1)
    changes: dict

    @field_validator("changes")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("changes must contain at least one field to update")
        return v


class SuiteCreateArgs(_Args):
    name: str


class AddScriptsToSuiteArgs(_Args):
    suite_id: str
    script_ids: list[str]

    @field_validator("script_ids")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("script_ids must be a non-empty array")
        return v


class RunExecutionConfiguration(_Args):
    # Mirrors automationhq-frontend-v2's ExecutionConfigurationSchema — the same shape backs
    # BOTH the Run TestBot dialog (POST /bots/{id}/execute) and the scheduler dialogs
    # (schedule-recurring / schedule-once-at): AddScheduler.tsx's own INITIAL_FORM_VALUES carry
    # the identical osType/browserVersion/gridId/resolution/timeout/etc. fields, confirmed by
    # reading it directly — this is not an assumption. baseUrl/browser/gridId are hard-required
    # there; the numeric bounds are the same ones the form enforces (timeouts 1-300s, delay
    # 0-30s, retries 0-3). Values for browser/gridId must come from list_browsers/list_grids —
    # never invented.
    baseUrl: str = Field(min_length=1)
    browser: str = Field(min_length=1)
    gridId: str = Field(min_length=1)
    # Server-required (confirmed live: 400 "Browser Version is required in browser configuration
    # at index 0" when empty) — valid values come from get_grid_capabilities; "latest" works on
    # plain Selenium grids.
    browserVersion: str = Field(min_length=1)
    # Server-required too (400 "OS Type is required") — from get_grid_capabilities' platforms.
    osType: str = Field(min_length=1)

    @field_validator("baseUrl")
    @classmethod
    def _base_url_is_environment_id(cls, v):
        # Despite the name, the backend treats baseUrl as an ENVIRONMENT ID
        # (TestRemoteExecutionRepository.getBaseUrlName does environmentRepository.findById and
        # hard-throws "Environment not found for this id" for anything else). A raw URL passes
        # some code paths via fallback and then kills the run minutes later in reporting —
        # confirmed live 2026-07-13. Reject it upfront instead.
        if v.startswith(("http://", "https://")):
            raise ValueError(
                "baseUrl must be an Environment ID, not a URL — the backend resolves it via "
                "environment lookup. Pick one from list_environments, or create one for this "
                "app with create_environment(name, url) and pass its environmentId."
            )
        return v
    gridUrl: str | None = None
    gridUrlForExecution: str | None = None
    # "Local Machine Resolution" only means something for the local-agent grid ("use whatever
    # resolution my own machine has") — a real cloud grid like TestingBot rejects it outright
    # (confirmed live: "Invalid screen-resolution specified: Local Machine Resolution", 500 on
    # session creation). So this can't be a static default here; the dispatch layer in
    # mcp_server.py fills it in AFTER determining whether the target grid is actually local (see
    # _fill_resolution_default) — left unset otherwise, matching the pre-existing (working)
    # behavior for cloud grids.
    resolution: str | None = None
    # type has no equivalent grid-dependent trap — every grid this platform talks to is Web-only
    # today (no API/mobile-only bot type flows through execute_bot's execution_configuration).
    type: str = "Web"
    headless: bool = False
    timeout: int = Field(default=60, ge=1, le=300)
    waitForElementTimeout: int = Field(default=30, ge=1, le=300)
    delayBetweenSteps: int = Field(default=0, ge=0, le=30)
    numberOfRetries: int = Field(default=0, ge=0, le=3)
    screenshotAfterEachStep: bool = False
    screenshotOnError: bool = True
    screenshotOnFinish: bool = True
    # Video capture is gated server-side on THIS flag, not on the grid's own videoRecording
    # field: RemoteExecutionStatusHandler only looks for a recording when
    # executionConfiguration.isVideoRecording() is true. Absent here, it defaults false, so every
    # MCP-triggered run produced IterationResult.videoUrl = null — confirmed live on a Selenium
    # Hub run whose GRID advertises videoRecording: true, on both a passing and a failing
    # iteration. Not defaulted true: the post-run path polls storage for the file, which costs
    # time on every run whether or not anyone wants the recording.
    videoRecording: bool = False
    excludeToBeRepairedTest: bool = False
    closeBrowserAfterEachExecution: bool = True
    customProperties: list = Field(default_factory=list)
    # Matches the frontend's own branch-fallback pattern (activeBranchByProject[projectId] ?? 'main').
    targetBranchName: str = "main"
    scriptBranchOverrides: dict[str, str] | None = None
    profileId: str | None = None


class ExecuteBotArgs(_Args):
    bot_id: str
    execution_configuration: RunExecutionConfiguration
    name: str | None = None
    profile_id: str | None = None
    partial_execution: bool = False


class SchedulerCreateArgs(_Args):
    # Mirrors automationhq-frontend-v2's SchedulerSchema (the TestBot clock-icon dialog's real
    # zod contract, which POSTs to test-management-services' /rest/api/schedulers) — NOT the
    # background-v2-services scheduling endpoints this tool used before, which write to a
    # different, UI-invisible mechanism that doesn't reliably fire (confirmed live 2026-07-15).
    bot_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    emails: list[str] = Field(default_factory=list)
    # The cron expression itself (backend field name recurringRule) — required server-side (a
    # null value here throws a live NullPointerException in the backend's own
    # recurringRule.replace(...) call). Use convert_text_to_cron for a human-language -> cron
    # helper rather than hand-writing one.
    cron: str = Field(min_length=1)
    execution_configuration: RunExecutionConfiguration


class _TestSuiteRef(_Args):
    testSuiteId: str = Field(min_length=1)
    name: str = ""


class _BotType(_Args):
    type: str = Field(min_length=1)
    value: str = Field(min_length=1)


class TestBotCreateArgs(_Args):
    # Mirrors the frontend's TestBotSchema: name 1-120, description <=500, at least one linked
    # Test Suite. botType is required on the form but the server defaults it to REGRESSION_TEST,
    # so it stays optional here. A TestBot has NO browser/grid config — that's execute-time.
    name: str = Field(min_length=1, max_length=120)
    test_suites: list[_TestSuiteRef] = Field(min_length=1)
    description: str = Field(default="", max_length=500)
    bot_type: _BotType | None = None
    folder_id: str | None = None
    profile_id: str | None = None
    number_of_retries: int = Field(default=0, ge=0, le=3)


class ApiCollectionCreateArgs(_Args):
    name: str


class ApiRequestCreateArgs(_Args):
    name: str
    method: str
    url: str

    @field_validator("method")
    @classmethod
    def _method_enum(cls, v):
        if v not in VALID_API_METHODS:
            raise ValueError(f"method must be one of {sorted(VALID_API_METHODS)}")
        return v


class WorkflowCreateArgs(_Args):
    name: str
    workflow_list: list[dict]

    @field_validator("workflow_list")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("workflow_list must be a non-empty array — a workflow with no chained requests isn't useful")
        return v


class EpicCreateArgs(_Args):
    name: str


class StoryCreateArgs(_Args):
    epic_id: str
    name: str


class AddGlobalParameterArgs(_Args):
    name: str
    value: str
    description: str | None = None


class CreateConfigVaultSecretArgs(_Args):
    name: str
    value: str
    description: str | None = None


class UpdateConfigVaultSecretArgs(_Args):
    secret_id: str
    value: str | None = None
    description: str | None = None

    @model_validator(mode="after")
    def _at_least_one_change(self):
        if self.value is None and self.description is None:
            raise ValueError("update_config_vault_secret needs at least one of value/description to change")
        return self


class PromoteRecordedScriptArgs(_Args):
    recorded_script_id: str
    # Required by our platform-wide rule AND by the server itself for a first-time promotion
    # (RecordedScriptService throws "storyId is required for first-time promotion") — promoting
    # produces a real TestScript, and scripts without a story are invisible in the Table View.
    story_id: str

    @field_validator("recorded_script_id", "story_id")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("must be a non-empty id")
        return v


def _validate_common_function_name(v: str) -> str:
    # Ported from automationhq-frontend-v2's CommonFunctionSchema (zod): trimmed, 1-120 chars,
    # letters/digits/spaces/hyphens only.
    v = v.strip()
    if not (1 <= len(v) <= 120):
        raise ValueError("name must be 1-120 characters")
    if not COMMON_FUNCTION_NAME_RE.match(v):
        raise ValueError("name may only contain letters, digits, spaces, and hyphens")
    return v


class CommonFunctionReturnType(_Args):
    # ReturnTypeSchema: name may be empty (defaults ''), type is required non-empty, array bool.
    type: str
    name: str = ""
    array: bool = False

    @field_validator("type")
    @classmethod
    def _type_nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("return_type.type must be a non-empty string (e.g. 'String')")
        return v


class CommonFunctionCreateArgs(_Args):
    name: str
    website_id: str
    status: str
    return_type: CommonFunctionReturnType
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name_rules(cls, v):
        return _validate_common_function_name(v)

    @field_validator("website_id", "status")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("description")
    @classmethod
    def _description_len(cls, v):
        if v is not None and len(v) > 600:
            raise ValueError("description must be at most 600 characters")
        return v


class CommonFunctionUpdateArgs(_Args):
    common_function_id: str
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name_rules(cls, v):
        if v is None:
            return v
        return _validate_common_function_name(v)

    @field_validator("description")
    @classmethod
    def _description_len(cls, v):
        if v is not None and len(v) > 600:
            raise ValueError("description must be at most 600 characters")
        return v


VALID_BRANCH_STRATEGIES = {"FROM_BRANCH", "FROM_CURRENT"}


class CreateBranchArgs(_Args):
    branch_name: str
    strategy: str | None = None

    @field_validator("branch_name")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("branch_name must be non-empty")
        return v

    @field_validator("strategy")
    @classmethod
    def _strategy_enum(cls, v):
        if v is not None and v not in VALID_BRANCH_STRATEGIES:
            raise ValueError(f"strategy must be one of {sorted(VALID_BRANCH_STRATEGIES)}")
        return v


class CommitBranchArgs(_Args):
    branch_name: str
    message: str

    @field_validator("branch_name", "message")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v


class CreatePullRequestArgs(_Args):
    source_branch: str
    target_branch: str
    title: str

    @field_validator("source_branch", "target_branch", "title")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v

    @model_validator(mode="after")
    def _different_branches(self):
        if self.source_branch == self.target_branch:
            raise ValueError("source_branch and target_branch must differ")
        return self


# From ahq-data-commons' Permission enum — exactly these five, matching the frontend's
# TRolePermission. VIEW is deliberately independent: it is NOT implied by the others and must
# never be auto-added when other permissions are granted.
VALID_ROLE_PERMISSIONS = {"VIEW", "EXECUTE", "EDIT", "DELETE", "SHARE"}


def _validate_permissions(v: list) -> list:
    if not v:
        raise ValueError("permissions must be a non-empty array")
    unknown = set(v) - VALID_ROLE_PERMISSIONS
    if unknown:
        raise ValueError(
            f"unknown permission(s) {sorted(unknown)} — valid values: {sorted(VALID_ROLE_PERMISSIONS)}"
        )
    return v


class ProjectRoleCreateArgs(_Args):
    role_name: str
    permissions: list[str]

    @field_validator("role_name")
    @classmethod
    def _name_nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("role_name must be non-empty")
        return v

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v):
        return _validate_permissions(v)


class ProjectRoleUpdateArgs(_Args):
    role_id: str
    permissions: list[str]

    @field_validator("permissions")
    @classmethod
    def _perms(cls, v):
        return _validate_permissions(v)


class AssignRoleArgs(_Args):
    role_id: str
    user_id: str

    @field_validator("role_id", "user_id")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("must be a non-empty id")
        return v


# Archive Manager entity types — mirrors UserClient.ARCHIVE_ENTITY_PATHS plus "recorded_script"
# (whose archive endpoints live in test-management, routed separately by the dispatcher).
ARCHIVE_ENTITY_TYPES = {
    "epic", "story", "website", "page", "locator",
    "test_script", "test_suite", "test_bot", "test_bot_folder", "recorded_script",
}


class ArchiveListArgs(_Args):
    entity_type: str

    @field_validator("entity_type")
    @classmethod
    def _known_entity(cls, v):
        if v not in ARCHIVE_ENTITY_TYPES:
            raise ValueError(f"entity_type must be one of {sorted(ARCHIVE_ENTITY_TYPES)}")
        return v


class ArchiveAssetArgs(ArchiveListArgs):
    asset_id: str

    @field_validator("asset_id")
    @classmethod
    def _nonblank(cls, v):
        if not v or not v.strip():
            raise ValueError("asset_id must be a non-empty id")
        return v


# Tool name -> validator model. Only tools that create/schedule something need an entry; read-only
# tools have nothing to validate.
VALIDATORS: dict[str, type[_Args]] = {
    "create_test_script": TestScriptCreateArgs,
    "create_suite": SuiteCreateArgs,
    "add_scripts_to_suite": AddScriptsToSuiteArgs,
    "schedule_bot_recurring": SchedulerCreateArgs,
    "create_api_collection": ApiCollectionCreateArgs,
    "create_api_request": ApiRequestCreateArgs,
    "create_workflow": WorkflowCreateArgs,
    "create_epic": EpicCreateArgs,
    "create_story": StoryCreateArgs,
    "promote_recorded_script": PromoteRecordedScriptArgs,
    "create_branch": CreateBranchArgs,
    "commit_branch": CommitBranchArgs,
    "create_pull_request": CreatePullRequestArgs,
    "create_project_role": ProjectRoleCreateArgs,
    "update_project_role_permissions": ProjectRoleUpdateArgs,
    "assign_project_role": AssignRoleArgs,
    "list_archived_assets": ArchiveListArgs,
    "restore_asset": ArchiveAssetArgs,
    "permanently_delete_asset": ArchiveAssetArgs,
    "create_common_function": CommonFunctionCreateArgs,
    "update_common_function": CommonFunctionUpdateArgs,
    "add_global_parameter": AddGlobalParameterArgs,
    "create_config_vault_secret": CreateConfigVaultSecretArgs,
    "update_config_vault_secret": UpdateConfigVaultSecretArgs,
    "create_test_bot": TestBotCreateArgs,
    "execute_bot": ExecuteBotArgs,
    "add_test_steps": AddTestStepsArgs,
    "update_test_script": UpdateTestScriptArgs,
}


def format_validation_error(tool_name: str, error: ValidationError) -> str:
    lines = [f"Invalid arguments for {tool_name}:"]
    for err in error.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
