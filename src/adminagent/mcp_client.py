"""mcp_client.py — single async MCP caller used by all workflows and agent tools."""

#from __future__ import annotations
import json
import os
import httpx

MCP_BASE_URL = os.getenv("MCP_BASE_URL", "http://localhost:8000")
MCP_TOKEN    = os.getenv("MCP_TOKEN", "")

# add at top after existing imports:
from opentelemetry import trace
tracer = trace.get_tracer(__name__)

# wrap inside call_mcp():
async def call_mcp(tool_name: str, args: dict) -> dict:
    with tracer.start_as_current_span(f"mcp.{tool_name}") as span:
        span.set_attribute("tool", tool_name)
        span.set_attribute("args", json.dumps(args)[:500])
        
        # span.set_attribute("result", json.dumps(result)[:500])
async def call_mcp(tool_name: str, args: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if MCP_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_TOKEN}"
    payload = {
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": tool_name, "arguments": args}, "id": 1,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{MCP_BASE_URL}/mcp", json=payload, headers=headers)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                return {"error": body["error"].get("message", "unknown")}
            result = body.get("result", {})
            if "content" in result and result["content"]:
                try:
                    return json.loads(result["content"][0]["text"])
                except Exception:
                    return {"text": result["content"][0]["text"]}
            return result
        except Exception as e:
            return {"error": str(e)}
