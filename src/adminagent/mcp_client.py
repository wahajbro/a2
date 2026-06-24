"""mcp_client.py — single async MCP caller used by all workflows and agent tools."""

import json
import os
import httpx
from opentelemetry import trace

MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8000")
MCP_TOKEN    = os.getenv("MCP_TOKEN", "")
tracer = trace.get_tracer(__name__)


async def call_mcp(tool_name: str, args: dict) -> dict:
    with tracer.start_as_current_span(f"mcp.{tool_name}") as span:
        span.set_attribute("tool", tool_name)
        span.set_attribute("args", json.dumps(args)[:500])

        headers = {"Content-Type": "application/json"}
        if MCP_TOKEN:
            headers["Authorization"] = f"Bearer {MCP_TOKEN}"
        payload = {
            "jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": tool_name, "arguments": args}, "id": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"{MCP_BASE_URL}/mcp", json=payload, headers=headers)
                r.raise_for_status()
                body = r.json()
                if "error" in body:
                    result = {"error": body["error"].get("message", "unknown")}
                else:
                    inner = body.get("result", {})
                    if "content" in inner and inner["content"]:
                        try:
                            result = json.loads(inner["content"][0]["text"])
                        except Exception:
                            result = {"text": inner["content"][0]["text"]}
                    else:
                        result = inner
        except Exception as e:
            result = {"error": str(e)}

        span.set_attribute("result", json.dumps(result)[:500])
        return result