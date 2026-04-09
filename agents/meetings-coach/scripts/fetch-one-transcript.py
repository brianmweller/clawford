#!/usr/bin/env python3
"""Fetch a single Krisp transcript by meeting_id. Debug/test utility."""
import asyncio, json, sys
from pathlib import Path

KRISP_MCP_URL = "https://mcp.krisp.ai/mcp"
TOKEN_DIR = Path.home() / ".openclaw" / "meetings-coach-workspace" / "cache" / "krisp-tokens"

class FS:
    def __init__(self, d):
        self._d = Path(d)
    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        p = self._d / "tokens.json"
        return OAuthToken.model_validate(json.loads(p.read_text())) if p.exists() else None
    async def set_tokens(self, t):
        (self._d / "tokens.json").write_text(t.model_dump_json(indent=2))
    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        p = self._d / "client_info.json"
        return OAuthClientInformationFull.model_validate(json.loads(p.read_text())) if p.exists() else None
    async def set_client_info(self, c):
        (self._d / "client_info.json").write_text(c.model_dump_json(indent=2))

async def main(meeting_id):
    import httpx
    from mcp import ClientSession
    from mcp.client.auth.oauth2 import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata

    auth = OAuthClientProvider(
        server_url=KRISP_MCP_URL,
        client_metadata=OAuthClientMetadata(
            redirect_uris=["http://localhost:19823/callback"],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name="Murphy Debug",
            scope="read",
        ),
        storage=FS(TOKEN_DIR),
        redirect_handler=None, callback_handler=None, timeout=300.0,
    )
    hc = httpx.AsyncClient(auth=auth, timeout=httpx.Timeout(30, read=120))
    try:
        async with streamable_http_client(KRISP_MCP_URL, http_client=hc) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                resp = await s.call_tool("get_document", arguments={"documentId": meeting_id})
                for c in resp.content:
                    if hasattr(c, "text"):
                        print(c.text)
    finally:
        await hc.aclose()

if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "019d6dee89c770a180f5119d89d90829"
    asyncio.run(main(mid))
