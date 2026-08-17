import json
import time

from starlette.datastructures import Headers

from src.config.credentials import AhqCredentials, decode_ahq_token
from src.hosted.oauth_provider import AhqTokenVerifier


class DualAuthMiddleware:
    """
    Auth gate for the /mcp mount, accepting BOTH hosted auth forms:

    - `Authorization: Bearer <our blob>` (OAuth clients — Claude Desktop/claude.ai, Inspector):
      verified by AhqTokenVerifier; the AHQ credentials sealed inside the token are stashed in
      scope["ahq_credentials"] for _resolve_clients.
    - `X-API-AUTH-KEY` + `projectId` headers (pre-OAuth clients — Cursor/VS Code/Claude Code
      config with headers): passed straight through; _resolve_clients keeps building
      credentials from headers exactly as before.

    The SDK's RequireAuthMiddleware can't be used here because it 401s everything without a
    Bearer token, which would kill the legacy header path. The 401 shape (JSON body +
    WWW-Authenticate with resource_metadata) mirrors RequireAuthMiddleware._send_auth_error —
    that header is what makes MCP clients discover the OAuth flow (RFC 9728).
    """

    def __init__(self, app, verifier: AhqTokenVerifier, resource_metadata_url: str, base_url: str):
        self.app = app
        self.verifier = verifier
        self.resource_metadata_url = resource_metadata_url
        self.base_url = base_url  # the server's own AHQ_BASE_URL, never caller-supplied

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS":
            # CORS preflights are answered by the app-level CORSMiddleware; never 401 them.
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            access = await self.verifier.verify_token(auth[7:])
            if access is None or (access.expires_at and access.expires_at < time.time()):
                # Not one of our OAuth blobs — but it may be a first-party caller presenting the
                # signed-in user's own AHQ platform JWT (see AhqCredentials.from_headers). Those
                # arrive with org-id + projectId, which an OAuth client never sends because its
                # credentials are sealed inside the token instead. Fall through to the header
                # path rather than 401ing, or that path is unreachable behind this middleware.
                #
                # Safe for the same reason the X-API-AUTH-KEY path below is: nothing here trusts
                # the credential. It is replayed to the AHQ gateway on every downstream call, and
                # the gateway validates it there — a forged token buys a 401 from AHQ, not data.
                if headers.get("org-id") and headers.get("projectid"):
                    await self.app(scope, receive, send)
                    return
                await self._send_auth_error(send, "invalid_token", "Token is invalid or expired")
                return
            scope["ahq_credentials"] = AhqCredentials(
                # access.base_url was resolved from the AHQ token's own urlDetails claim at
                # consent time (dev vs prod gateway) — self.base_url is only a fallback for
                # tokens issued before this existed.
                base_url=access.base_url or self.base_url,
                api_token=access.ahq_token,
                org_id=access.org_id,
                project_id=access.project_id,
                # Which credential got sealed in decides how it must be presented downstream.
                # Every API token carries tokenType (ORGANIZATION or USER — the consent page
                # rejects anything else); the JWT that password sign-in issues never does, and
                # has to go back as Authorization: Bearer to reach the gateway's user-aware path.
                auth_scheme=(
                    "api-key" if decode_ahq_token(access.ahq_token).get("tokenType") else "bearer"
                ),
            )
            await self.app(scope, receive, send)
            return

        if headers.get("x-api-auth-key"):
            await self.app(scope, receive, send)
            return

        await self._send_auth_error(send, "invalid_token", "Authentication required")

    async def _send_auth_error(self, send, error: str, description: str) -> None:
        www_authenticate = (
            f'Bearer error="{error}", error_description="{description}", '
            f'resource_metadata="{self.resource_metadata_url}"'
        )
        body = json.dumps({"error": error, "error_description": description}).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", www_authenticate.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
