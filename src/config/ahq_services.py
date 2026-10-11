
import os
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _either(name: str) -> Field:
    """Accept TESTBOTS_<NAME> or AHQ_<NAME>, preferring the first.

    The three credentials below are the only settings a plugin user types by hand, so they are
    the only ones the rebrand can rename without a coordinated deploy — every other AHQ_MCP_*
    setting is written by DevOps into a ConfigMap. Renaming outright would strand every existing
    .env silently: a missing token reads as "not configured", which looks like a broken install
    rather than a renamed variable. Same approach as the connector cutover, which brought
    api-dev.testbots.ai up while the automationhq.ai host kept answering.
    """
    return Field("", validation_alias=AliasChoices(f"testbots_{name}", f"ahq_{name}"))

# The directory containing pyproject.toml / .env — resolved from this file's location, NOT the
# process cwd. Claude Code (and other MCP clients) launch the server with an arbitrary cwd (in
# practice the user's project directory), so a cwd-relative env_file=".env" silently finds
# nothing and the server runs with empty credentials against the default URL — every tool then
# "succeeds" with the web app's HTML instead of API data. Found live during the first teammate
# plugin install (2026-07-13).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_dirs() -> tuple[Path, ...]:
    # Ordered lowest→highest precedence (pydantic-settings: later files win); real environment
    # variables (TESTBOTS_API_TOKEN / AHQ_API_TOKEN etc.) beat every file:
    #   1. ~/.ahq/.env — the pre-rebrand credentials home, still read so an existing install
    #      keeps working untouched. Lowest precedence, so a user who creates the new path wins.
    #   2. ~/.testbots/.env — stable, version-INDEPENDENT credentials home. Each plugin version
    #      installs into a fresh folder with no .env, so credentials placed only inside a plugin
    #      folder die on every upgrade (bit a user live, 2026-07-13). This path survives.
    #   3. next to this package (repo checkout, or the plugin root when run via `-m` from there)
    #   4. TESTBOTS_MCP_HOME / AHQ_MCP_HOME — set by the plugin's .mcp.json to
    #      ${CLAUDE_PLUGIN_ROOT}; covers the pip-installed case where this file lives in
    #      site-packages and (3) points nowhere useful
    #   5. process cwd, as a manual override
    dirs = [
        Path.home() / ".ahq",
        Path.home() / ".testbots",
        REPO_ROOT,
    ]
    mcp_home = os.environ.get("TESTBOTS_MCP_HOME") or os.environ.get("AHQ_MCP_HOME")
    if mcp_home:
        dirs.append(Path(mcp_home))
    dirs.append(Path("."))
    return tuple(dirs)


def _clean_profile(raw: str) -> str:
    """A profile name becomes a filename suffix, so reject anything that could name a different
    file rather than sanitising it into something nobody asked for."""
    name = raw.strip().strip("\"'")
    return name if name and all(c.isalnum() or c in "-_" for c in name) else ""


def active_profile(dirs: tuple[Path, ...] | None = None) -> str:
    """The active configuration profile ("dev", "prod", ...) — Spring's spring.profiles.active.

    Resolved from a real environment variable first, then from whichever base .env names one
    (highest-precedence file wins). The file fallback is the important half here: this server is
    launched by the MCP client, not from a shell, so an exported variable is the awkward path
    rather than the obvious one. Empty means no profile, which is exactly the behaviour before
    profiles existed — an install that never sets one loads the same files it always did.
    """
    if raw := (os.environ.get("TESTBOTS_ENV") or os.environ.get("AHQ_ENV") or ""):
        return _clean_profile(raw)
    for directory in reversed(dirs or _env_dirs()):
        try:
            text = (directory / ".env").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().upper() in ("TESTBOTS_ENV", "AHQ_ENV"):
                if name := _clean_profile(value.split("#")[0]):
                    return name
    return ""


def _env_file_candidates() -> tuple[str, ...]:
    dirs = _env_dirs()
    candidates = [str(d / ".env") for d in dirs]
    # Every base file first, then every profile file, so an active profile beats an unprofiled
    # value regardless of which directory each came from. Same rule as Spring, where
    # application-<profile>.properties overrides application.properties instead of being ranked
    # against it by location — the alternative (profile file directly above its own base file)
    # lets ~/.testbots/.env quietly outrank ~/.ahq/.env.prod, which is not what "prod is active"
    # can be allowed to mean.
    if profile := active_profile(dirs):
        candidates += [str(d / f".env.{profile}") for d in dirs]
    return tuple(candidates)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file_candidates(),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # No default on purpose: the only guessable value would be the web frontend, which is never
    # correct for API calls. Empty + the fail-fast guard in mcp_server beats silently wrong.
    ahq_base_url: str = _either("base_url")
    # Required for stdio mode (loaded from .env); left empty in hosted HTTP mode, where every
    # request supplies its own credentials via headers (AhqCredentials.from_headers) instead —
    # the container itself never holds a single tenant's token/project.
    ahq_api_token: str = _either("api_token")
    # No ahq_org_id here on purpose — org_id is always derived from the token's own
    # organizationId claim (see AhqCredentials.from_settings), never independently configured.
    # A stale/mismatched org_id here would silently write real data into the wrong organization,
    # since the gateway doesn't validate that a request's org-id header matches the token's claim.
    ahq_project_id: str = _either("project_id")
    # Declared, not read: active_profile() resolves the profile before any of these files load,
    # so it cannot come from here. The field exists because model_config forbids extra keys —
    # without it, a TESTBOTS_ENV line in a .env file is a hard startup failure rather than a
    # setting.
    testbots_env: str = _either("env")
    llm_api_key: str = ""
    # Grace period check_local_agent_status waits out the first time it sees the local agent
    # online, before reporting ready — the agent's own async startup (token revalidation,
    # chromedriver resolution) can still be in progress even though it already answers /ping.
    # Stdio-only (check_local_agent_status is unavailable in hosted mode).
    ahq_local_agent_warmup_seconds: int = 15
    # Which slice of the 136-tool surface to advertise, e.g. "core" or "core,api" (see
    # src/tool_groups.py). Empty = all of them, so nothing changes for anyone who doesn't set it.
    # Stdio only — hosted clients pass ?profile= on the MCP URL, since they configure a URL and
    # not this process's environment.
    ahq_mcp_tool_profile: str = _either("mcp_tool_profile")

    # --- Hosted (HTTP) mode only — all defaulted so stdio users and tests never notice them ---
    # Public URL this service is reachable at THROUGH the gateway, including the route prefix
    # (StripPrefix removes it before requests reach us, but every URL we advertise to OAuth
    # clients must include it), e.g. https://api-dev.automationhq.ai/ahq-mcp-server.
    # Empty -> http://localhost:8000 for local development.
    ahq_mcp_public_base_url: str = ""
    # Fernet passphrase for the stateless OAuth blobs (client_id / code / access / refresh).
    # REQUIRED in hosted mode and must be identical across replicas — ahq-mcp-http fails fast at
    # startup if empty. Never used in stdio mode.
    ahq_mcp_auth_secret: str = ""
    ahq_mcp_rate_limit_per_min: int = 60
    ahq_mcp_max_body_bytes: int = 2_000_000
    # Extra allowed OAuth redirect URIs beyond loopback + the Claude callbacks, comma-separated
    # (e.g. a future Cursor/Windsurf deep-link scheme).
    ahq_mcp_extra_redirect_uris: str = ""
    ahq_mcp_crawl_concurrency: int = 2
    # How long one crawl may keep opening new pages. Kept under the callers' own request timeouts
    # (the in-app assistant waits 180s), so a long crawl returns what it found instead of nothing.
    ahq_mcp_crawl_time_budget_s: int = 150
    # Extra allowed AHQ gateway base URLs a token's own urlDetails.baseUrl claim may resolve to,
    # beyond the two known hosts (dev/prod — see credentials.KNOWN_BASE_URLS), comma-separated
    # (e.g. a future staging environment).
    ahq_mcp_extra_base_urls: str = ""

    # Gateway route-id prefixes, one per backend service (StripPrefix=1 removes these before the
    # gateway forwards a request). Defaults match the shared SaaS gateway's naming convention.
    # A self-hosted deployment whose gateway uses a different naming convention overrides the
    # ones it needs; everything else keeps working unchanged since these are pure additive
    # defaults, not a profile/dict switch.
    ahq_gw_prefix_asset: str = "/ahq-asset-services"
    ahq_gw_prefix_test_mgmt: str = "/ahq-test-management-services"
    ahq_gw_prefix_background: str = "/ahq-background-v2-services"
    ahq_gw_prefix_config: str = "/ahq-config-services"
    ahq_gw_prefix_user_mgmt: str = "/ahq-user-management-services"
    ahq_gw_prefix_executor: str = "/ahq-test-bot-executor-services"
    ahq_gw_prefix_standalone: str = "/ahq-standalone-local-v2-services"
    ahq_gw_prefix_managed_test: str = "/mtaf-core"
    ahq_gw_prefix_virt_client: str = "/mtaf-sv-client"
    ahq_gw_prefix_virt_server: str = "/mtaf-sv-server"
    ahq_gw_prefix_cdct: str = "/mtaf-cdct"
    ahq_gw_prefix_auth: str = "/ahq-auth-services"
    ahq_gw_prefix_email: str = "/ahq-email-v2-services"

    # Consent-page branding for a self-hosted deployment target — mirrors the
    # partner.display-name / partner.logo-url pattern other AHQ services already use for PDF
    # reports (e.g. ahq-test-management-services' application-aviso.properties, "CA UTAP").
    # Empty -> AutomationHQ's own name/logo/color, unchanged from today.
    ahq_mcp_partner_display_name: str = ""
    ahq_mcp_partner_logo_url: str = ""
    ahq_mcp_partner_primary_color: str = ""

    def extra_base_urls(self) -> frozenset[str]:
        return frozenset(u.strip() for u in self.ahq_mcp_extra_base_urls.split(",") if u.strip())


settings = Settings()

# Which profile the values above actually came from. Read this rather than settings.testbots_env
# (see that field's comment) — this is the value that selected the files.
ACTIVE_PROFILE = active_profile()

# Gateway prefix constants, derived from settings so a non-SaaS deployment target can override
# them (see the ahq_gw_prefix_* fields above) without any code change.
ASSET_SVC         = settings.ahq_gw_prefix_asset
TEST_MGMT_SVC     = settings.ahq_gw_prefix_test_mgmt
BACKGROUND_SVC    = settings.ahq_gw_prefix_background
CONFIG_SVC        = settings.ahq_gw_prefix_config
USER_MGMT_SVC     = settings.ahq_gw_prefix_user_mgmt
EXECUTOR_SVC      = settings.ahq_gw_prefix_executor
LOCAL_EXEC_SVC    = "/test-local-execution-services"  # NOT gateway-routed — runs on the user's machine, see local_exec_client.py
STANDALONE_SVC    = settings.ahq_gw_prefix_standalone
MANAGED_TEST_SVC  = settings.ahq_gw_prefix_managed_test   # gateway route id, NOT the repo name "managed-testing-service-core"
VIRT_CLIENT_SVC   = settings.ahq_gw_prefix_virt_client    # gateway route id, NOT the repo name "managed-testing-virtualization-client"
VIRT_SERVER_SVC   = settings.ahq_gw_prefix_virt_server    # gateway route id, NOT the repo name "managed-testing-virtualization-server"
CDCT_SVC          = settings.ahq_gw_prefix_cdct           # gateway route id, NOT the repo name "mtaf-cdct-core"
AUTH_SVC          = settings.ahq_gw_prefix_auth
EMAIL_SVC         = settings.ahq_gw_prefix_email
