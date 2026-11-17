"""Minimal MCP server exposing the Phase 2 retrieval tools. Uses the kb_reader (SELECT-only) role."""
from __future__ import annotations
import functools
import inspect

from mcp.server.mcpserver import MCPServer

from .. import db, tools


def _wrap(fn):
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]            # drop `conn`

    @functools.wraps(fn)
    def w(*a, **k):
        with db.connect("reader") as conn:
            try:
                return fn(conn, *a, **k)
            except tools.ToolError as e:
                return {"error": str(e)}
    w.__signature__ = sig.replace(parameters=params)
    w.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "conn"}
    return w


def build() -> MCPServer:
    server = MCPServer("repo-kb")
    for fn in tools.TOOLS:
        server.add_tool(_wrap(fn), name=fn.__name__, description=(fn.__doc__ or fn.__name__).strip().splitlines()[0])
    return server


def main():
    build().run()


if __name__ == "__main__":
    main()
