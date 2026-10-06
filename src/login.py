"""`testbots-login` — sign in once, store a long-lived API token.

Why this exists rather than putting a password in .env like the hosted connector does: the
connector never stores the password (`sign_in`'s docstring says so — it lives for the duration of
one HTTP call). Stdio has no browser and no session, so "authenticate with a password" there
would mean keeping the password on disk permanently, to be replayed on every server start. That
is strictly worse than a token, which is revocable, scoped, and cannot change the account's own
password or open the web UI.

So this keeps the interactive part and drops the storage: prompt for the password, exchange it
for a sign-in JWT, use that JWT once to mint an API token, write only the token. The password is
never written anywhere and is not held after the first call.

The token is a personal (USER) one by default. Organization tokens share one limit of 5 across
everyone in the organization, so a person who had created a single token was refused because
colleagues held the other four. Personal tokens are limited per person, so the check is against
the signer's own list - the one they see under API Tokens. `--org-token` still mints an
organization token for whoever wants one.
"""
import asyncio
import datetime
import getpass
import os
import sys
from pathlib import Path

import httpx

from src.clients.base_client import AhqApiError
from src.clients.user_client import UserClient
from src.config.ahq_services import _clean_profile, settings
from src.config.credentials import PROFILE_BASE_URLS, AhqCredentials, decode_ahq_token
from src.hosted.consent import _normalize_projects, sign_in

CREDENTIALS_HOME = Path.home() / ".testbots"
ENV_PATH = CREDENTIALS_HOME / ".env"

# Plain ASCII, like _NEEDS_TERMINAL below: the Windows console codepage turns an em dash into a
# replacement character.
_USAGE = """testbots-login - sign in and store an API token.

  testbots-login                     sign in to the configured gateway
  testbots-login --env=prod          sign in to prod, saving to ~/.testbots/.env.prod
  testbots-login --env=dev           the same for dev
  testbots-login --use=prod          switch profiles without signing in again
  testbots-login --base-url=URL      a gateway not covered by a named profile
  testbots-login --force             mint another token even if one is already stored
  testbots-login --org-token         mint an organization token instead of a personal one
"""

# Above this, the picker asks for a filter first. Chosen to fit a default terminal without
# scrolling, so the whole list stays visible once it is short enough to print.
_FILTER_THRESHOLD = 15

_NEEDS_TERMINAL = (
    "testbots-login needs an interactive terminal for the password prompt.\n"
    # Plain ASCII: the Windows console codepage mangles an em dash into a replacement char.
    "Run it in a terminal window, not through a pipe, a CI job, or an AI assistant\n"
    "session, none of which can prompt for a password without capturing it."
)


def _env_path(profile: str = "") -> Path:
    """Where a profile's credentials live. No profile keeps writing the file it always did."""
    return CREDENTIALS_HOME / f".env.{profile}" if profile else ENV_PATH


def _merge_env(path: Path, managed: dict) -> None:
    """Apply `managed`, preserving any unrelated lines already in the file."""
    existing = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key = line.split("=", 1)[0].strip().upper()
            # Drop the AHQ_* spellings too, or the old value survives alongside the new one and
            # which wins depends on precedence rather than on anything the user chose.
            if key in managed or key.replace("TESTBOTS_", "AHQ_", 1) in {
                    k.replace("TESTBOTS_", "AHQ_", 1) for k in managed}:
                continue
            existing.append(line)

    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(existing + [f"{k}={v}" for k, v in managed.items()]).strip() + "\n"
    path.write_text(body, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best effort; a no-op on Windows
    except OSError:
        pass


def _write_env(token: str, project_id: str, base_url: str, profile: str = "") -> Path:
    managed = {"TESTBOTS_API_TOKEN": token, "TESTBOTS_PROJECT_ID": project_id}
    # A profile file exists precisely to name its own environment, so pin the gateway there
    # unconditionally — that is what makes the file readable on its own and what stops a later
    # unprofiled default deciding where a prod token points. Without a profile the older, narrower
    # rule stands: pin only when the token cannot name the gateway itself, since a stale pin is
    # how an .env ends up on the wrong environment after the token is replaced.
    if base_url and (profile or base_url != settings.ahq_base_url):
        managed["TESTBOTS_BASE_URL"] = base_url

    path = _env_path(profile)
    _merge_env(path, managed)
    return path


def _activate(profile: str) -> int:
    """Point the base .env at a profile, so switching environments is not a manual file edit.

    The switch is written to the base file rather than exported, because nothing here launches
    the MCP server — the client does, and it inherits its own environment, not this shell's.
    """
    path = _env_path()
    if not (target := _env_path(profile)).exists():
        print(f"No {target} yet. Run: testbots-login --env={profile}")
        return 1
    _merge_env(path, {"TESTBOTS_ENV": profile})
    print(f"Profile {profile} is now active ({target}).")
    print("\nRestart Claude Code for it to take effect.")
    return 0


def _choose(projects: list) -> dict:
    if len(projects) == 1:
        print(f"\nProject: {projects[0]['name']}")
        return projects[0]

    # A long-lived account can hold roles on scores of projects, most of them throwaway
    # (one real org listed 88, of which 50+ were E2E_Project_<timestamp> fixtures). Printing
    # all of them scrolls the one you want off the screen, so narrow before listing.
    shown = projects
    while len(shown) > _FILTER_THRESHOLD:
        print(f"\n{len(shown)} projects.")
        raw = input("Type part of a name to narrow the list, or Enter to show all: ").strip()
        if not raw:
            break
        matches = [p for p in shown if raw.lower() in p["name"].lower()]
        if not matches:
            # Filter the full set, not the narrowed one, or a typo dead-ends the search.
            matches = [p for p in projects if raw.lower() in p["name"].lower()]
        if not matches:
            print(f"Nothing matches {raw!r}.")
            continue
        shown = matches

    print("\nChoose a project:")
    for i, p in enumerate(shown, 1):
        print(f"  {i}. {p['name']}")
    while True:
        raw = input(f"\nNumber [1-{len(shown)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        print("Not one of the options.")


async def _run(base_url: str, force: bool, profile: str = "", org_token: bool = False) -> int:
    env_path = _env_path(profile)
    if env_path.exists() and "TESTBOTS_API_TOKEN=" in env_path.read_text(encoding="utf-8") \
            and not force:
        # Minting is limited per organization and this endpoint does not revoke what it replaces,
        # so a login that silently re-mints burns a finite quota on every run.
        print(f"{env_path} already holds a token. Re-run with --force to mint another.")
        return 1

    # Only catches the redirected-stdin case, and only where isatty is trustworthy — Windows
    # reports NUL as a character device, so this is False there even with no console at all.
    # The EOFError below is what actually fires on Windows.
    if not sys.stdin.isatty():
        print(_NEEDS_TERMINAL)
        return 1

    # The gateway host is an implementation detail and currently still carries the old brand, so
    # show the product name instead. Naming an environment is different: anyone who picked one
    # needs to see which they actually got, or a prod/dev mix-up is silent.
    if profile:
        print(f"Signing in to {profile} ({base_url})\n")
    elif base_url == settings.ahq_base_url:
        print("Signing in to TestBots.ai\n")
    else:
        print(f"Signing in to {base_url}\n")
    try:
        email = input("Email: ").strip()
        # No usable console means getpass reads the console device and blocks rather than
        # falling back to a visible prompt. Refusing beats hanging, and both beat echoing the
        # password into whatever is capturing output — a CI log, or an assistant transcript.
        password = getpass.getpass("Password: ")
    except EOFError:
        print(f"\n{_NEEDS_TERMINAL}")
        return 1
    if not email or not password:
        print("Email and password are both required.")
        return 1

    async with httpx.AsyncClient() as http:
        jwt = await sign_in(http, base_url, email, password)
        if not jwt:
            print("\nUnable to log in. Your login details are incorrect, or your account has "
                  "been disabled.")
            return 1

        def client(org_id: str = "") -> UserClient:
            return UserClient(
                credentials=AhqCredentials(base_url=base_url, api_token=jwt, org_id=org_id,
                                           project_id="", auth_scheme="bearer"),
                http_client=http,
            )

        # registration_info, not /users/me: the latter 500s for a password JWT, which carries
        # only `sub`. This is also the only lookup that works before an organization is known.
        me = await client().registration_info(email)
        org_id, user_id = str(me.get("organizationId") or ""), str(me.get("userId") or "")
        if not org_id or not user_id:
            print("\nSigned in, but this account is not attached to an organization.")
            return 1

        # User-scoped first (what this person holds a role in), then org-wide, which is what
        # the consent flow already used. Without the fallback an account with few or no
        # per-project roles dead-ends on an organization full of projects: support@ has one
        # role, on a project whose orgId is null, while its organization holds 54 — so this
        # reported "no projects" where the hosted connector listed them all.
        projects = await client(org_id).list_projects_for_user(user_id)
        if not projects:
            try:
                projects = _normalize_projects(await client(org_id).list_projects())
            except Exception:
                projects = []
        if not projects:
            # Naming the organization matters: both lookups are org-scoped, so an account in a
            # different organization than the user expects reads as a broken login rather than
            # as the wrong org. Minting would not help either -- createOrgToken allows
            # belongsToOrg || isOrgCreator, so this same organization is the only one reachable.
            print(f"\nSigned in, but organization {org_id} has no projects, so there is "
                  f"nothing to connect to.\nIf you expected projects here, check you are in "
                  f"the organization you think you are.")
            return 1
        project = _choose(projects)

        label = f"testbots-mcp-server ({os.environ.get('COMPUTERNAME') or os.uname().nodename})"
        if org_token:
            try:
                result = await client(org_id).create_org_token(org_id, user_id, base_url, label=label)
            except AhqApiError as exc:
                # Per organization, low (5), shared by every member and never revoked on replace,
                # so an established org is usually at it. A personal token is the way through.
                if "token limit" not in str(exc).lower():
                    raise
                print(f"\n{org_id} has reached its limit of active organization API tokens - that\n"
                      f"limit is shared by everyone in the organization.\n\nRun this again without "
                      f"--org-token to get a personal token instead, or delete an\norganization token "
                      f"under Administration -> Settings -> API Tokens.")
                return 1
        else:
            # Checked against the signer's own tokens first, so a full list is reported plainly
            # rather than as a failed request - and other people's tokens never count here.
            status = await client(org_id).user_token_status(user_id)
            active = int(status.get("activeTokenCount") or 0)
            remaining = status.get("remainingTokens")
            if remaining is not None and int(remaining) <= 0:
                print(f"\nYou already have {active} active personal API tokens, which is the limit "
                      f"for one person.\n\nDelete one you no longer use in the web app under "
                      f"Administration -> Settings -> API Tokens,\nthen run this again. Nothing has "
                      f"been changed and your existing tokens still work.")
                return 1
            try:
                result = await client(org_id).create_user_token(user_id, base_url, label=label)
            except AhqApiError as exc:
                if "token limit" not in str(exc).lower():
                    raise
                print("\nYou have reached the limit of active personal API tokens.\n\nDelete one "
                      "under Administration -> Settings -> API Tokens, then run this again.")
                return 1

    token = result.get("token") or ""
    if not token:
        print(f"\nToken was not issued: {result}")
        return 1

    env_path = _write_env(token, project["id"], base_url, profile)

    # Read back from the token itself rather than echoing what we sent. The organization name
    # and expiry are only decided server-side, so this is the one thing that confirms what was
    # actually issued -- and the org UUID alone tells nobody which organization they landed in.
    claims = decode_ahq_token(token)
    org_name = claims.get("organizationName") or me.get("organizationName") or org_id
    person = " ".join(p for p in (me.get("firstName"), me.get("lastName")) if p).strip()
    expires = claims.get("exp")
    expiry = f" (expires {datetime.date.fromtimestamp(expires)})" if expires else ""

    print(f"\nSigned in as {person or email}.")
    print()
    if profile:
        print(f"  profile       {profile}")
        print(f"  gateway       {base_url}")
    print(f"  organization  {org_name}")
    print(f"  project       {project['name']}")
    kind = "organization" if org_token else "personal"
    print(f"  API token     {kind} token created{expiry}")
    print(f"  saved to      {env_path}")
    if (remaining := result.get("remainingTokens")) is not None:
        left = int(remaining)
        owner = "this organization" if org_token else "your account"
        print(f"\n{left} {'token slot' if left == 1 else 'token slots'} left on {owner}.")
    if profile:
        # Writing the credentials is not the same as selecting them: a profile file is inert
        # until something names it. Switching silently here is exactly the prod/dev mix-up this
        # command is otherwise careful to make visible.
        print(f"\nThis profile is not active yet. To switch to it:\n\n"
              f"    testbots-login --use={profile}\n")
    print("\nYou are all set. Restart Claude Code, then run /mcp -- it should show\n"
          "testbots-mcp-server connected.")
    return 0


def _arg(args: list, flag: str, default: str = "") -> str:
    return next((a.split("=", 1)[1] for a in args if a.startswith(f"{flag}=")), default)


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(_USAGE)
        return 0

    if switch_to := _arg(args, "--use"):
        if not (profile := _clean_profile(switch_to)):
            print(f"{switch_to!r} is not a usable profile name.")
            return 1
        return _activate(profile)

    force = "--force" in args
    org_token = "--org-token" in args
    profile = _clean_profile(_arg(args, "--env"))
    if _arg(args, "--env") and not profile:
        print(f"{_arg(args, '--env')!r} is not a usable profile name.")
        return 1

    # An explicit --base-url wins so an environment with no named profile stays reachable; a
    # named profile is what supplies the gateway otherwise, since a password cannot name one.
    base_url = _arg(args, "--base-url") or PROFILE_BASE_URLS.get(profile) or settings.ahq_base_url
    if not base_url:
        print("No gateway URL. Pass --env=prod, --env=dev, or --base-url=<gateway>")
        return 1
    if profile and profile not in PROFILE_BASE_URLS and not _arg(args, "--base-url"):
        # Otherwise a made-up profile silently signs in to whatever the current default is and
        # writes the result into a file named after an environment it never contacted.
        print(f"Profile {profile!r} has no known gateway. Pass --base-url=<gateway> with it, "
              f"or use one of: {', '.join(PROFILE_BASE_URLS)}")
        return 1
    try:
        return asyncio.run(_run(base_url, force, profile, org_token))
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as exc:
        print(f"\n{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
