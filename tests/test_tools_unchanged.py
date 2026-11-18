"""What clients see of each tool must not change by accident.

`fixtures/tools_v1.json` records the tools of v1.0 (built on the MCP SDK's MCPServer):
names, order, read-only/destructive hints, descriptions (whitespace-normalised, since
FastMCP strips docstring indentation) and parameters. Regenerate it on purpose when a
tool really changes.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from fastmcp import Client

from obsidian_mcp.config import Settings
from obsidian_mcp.server import build_server

FIXTURE = Path(__file__).parent / "fixtures" / "tools_v1.json"


def _summary(tool) -> dict:
    d = tool.model_dump(mode="json", exclude_none=True, by_alias=True)
    schema = d["inputSchema"]
    return {
        "name": d["name"],
        "description": re.sub(r"\s+", " ", d["description"]).strip(),
        "annotations": {k: v for k, v in d.get("annotations", {}).items() if k != "title"},
        "parameters": {
            k: {kk: v[kk] for kk in ("type", "anyOf", "default") if kk in v}
            for k, v in schema["properties"].items()
        },
        "required": schema.get("required", []),
    }


def test_tools_match_v1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OBSIDIAN_VAULT", str(tmp_path))

    async def listed() -> list[dict]:
        async with Client(build_server(Settings())) as client:
            return [_summary(t) for t in await client.list_tools()]

    assert asyncio.run(listed()) == json.loads(FIXTURE.read_text(encoding="utf-8"))
