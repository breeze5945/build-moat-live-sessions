import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .storage import SessionLocal, init_db
from .scheduler import start_scheduler
from .tools import TOOL_DEFINITIONS, route_tool_call

server: Server = Server("task-scheduler")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOL_DEFINITIONS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    db = SessionLocal()
    try:
        result = await asyncio.to_thread(route_tool_call, name, arguments or {}, db)
    finally:
        db.close()
    return [TextContent(type="text", text=json.dumps(result, default=str, ensure_ascii=False))]


async def main() -> None:
    init_db()
    start_scheduler()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
