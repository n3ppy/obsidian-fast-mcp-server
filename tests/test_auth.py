"""Client registration is open to anyone: a flood of registrations must not log the
owner out."""

from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.shared.auth import OAuthClientInformationFull

from obsidian_mcp.auth import MAX_CLIENTS, VaultOAuthProvider
from obsidian_mcp.config import Settings

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def _client(client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(client_id=client_id, redirect_uris=[CALLBACK])


def test_registration_flood_keeps_logged_in_client(tmp_path: Path) -> None:
    provider = VaultOAuthProvider(
        Settings(
            transport="http",
            public_url="https://mcp.example.com",
            password="correct horse battery staple",
            state_dir=tmp_path,
        )
    )

    async def scenario() -> None:
        await provider.register_client(_client("owner"))
        provider._issue("owner", [], None)  # the owner has logged in
        await provider.register_client(_client("idle"))
        for i in range(MAX_CLIENTS * 2):
            await provider.register_client(_client(f"junk-{i}"))

        assert await provider.get_client("owner") is not None
        assert await provider.get_client("idle") is None
        # The newest registration is always kept, so a login in progress can finish.
        assert await provider.get_client(f"junk-{MAX_CLIENTS * 2 - 1}") is not None
        assert len(provider.store.data["clients"]) == MAX_CLIENTS

    asyncio.run(scenario())
