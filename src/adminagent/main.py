"""
main.py — WFCF Admin Agent entry point

Detects intent from user message, builds the right workflow, runs it.

Supported intents:
  - add_user_to_standard
  - add_user_to_subgroup
  - create_standard
  - create_subgroup
  - grant_revoke_path
  - remove_user
  - list_standards
  - list_members
"""

import asyncio
import json
import os
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from agent_framework_foundry import FoundryChatClient

from messages import WorkflowResult
from mcp_client import call_mcp
from workflow_runner import run_workflow

import add_user_to_standard
import add_user_to_subgroup
import create_standard
import create_subgroup
import grant_revoke_path
import remove_user

load_dotenv()

PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")


# ─────────────────────────────────────────────────────────────────────────────
# AGENT TOOLS (LLM can call these for list queries)
# ─────────────────────────────────────────────────────────────────────────────
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

configure_azure_monitor(
    connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
)
tracer = trace.get_tracer(__name__)

async def list_standards_tool() -> str:
    """List all available WFCF standards."""
    result = await call_mcp("list_standards", {})
    if "error" in result:
        return f"Error: {result['error']}"
    standards = result.get("standards", [])
    if not standards:
        return "No standards found."
    return json.dumps([
        {"name": s["name"], "display_name": s.get("display_name", s["name"])}
        for s in standards
    ])


# new
async def list_standard_members_tool(standard: str) -> str:
    """List current members of a standard. standard must be a valid slug like 'organic'. If not provided, ask the user first."""
    if not standard or len(standard) < 2:
        return "Error: Please provide a valid standard name (e.g. 'organic')."
    result = await call_mcp("list_standard_members", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    return json.dumps([
        {
            "name":  m.get("display_name", ""),
            "email": m.get("email", ""),
            "tier":  m.get("tier", ""),
        }
        for m in members
    ])

async def list_subgroups_tool(standard: str) -> str:
    """List subgroups of a standard."""
    result = await call_mcp("list_subgroups", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    sgs = result.get("subgroups", [])
    return json.dumps([
        {"slug": s["slug"], "display_name": s.get("display_name", s["slug"])}
        for s in sgs
    ])

async def list_subgroup_members_tool(standard: str, slug: str) -> str:
    """List members of a subgroup. Call this whenever user asks who is in a subgroup."""
    result = await call_mcp("list_subgroup_members", {"standard": standard, "slug": slug})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    if not members:
        return f"No members in subgroup '{slug}'."
    return json.dumps([
        {
            "name":  m.get("display_name", ""),
            "email": m.get("email", ""),
        }
        for m in members
    ])


async def list_subgroup_paths_tool(standard: str, slug: str) -> str:
    """List granted paths for a subgroup. Call this whenever user asks about paths."""
    result = await call_mcp("list_subgroup_paths", {"standard": standard, "slug": slug})
    if "error" in result:
        return f"Error: {result['error']}"
    paths = result.get("paths", [])
    if not paths:
        return f"No paths granted to '{slug}'."
    return "\n".join(paths)    

async def search_documents_tool(query: str, standard_family: str = "") -> str:
    """Search regulatory documents. Use when user asks about document content."""
    args = {"query": query}
    if standard_family:
        args["standard_family"] = standard_family
    result = await call_mcp("search_documents", args)
    if "error" in result:
        return f"Error: {result['error']}"
    chunks = result.get("chunks", [])
    if not chunks:
        return "No documents found."
    return json.dumps([
        {
            "title":    c.get("title", ""),
            "section":  c.get("section", ""),
            "content":  c.get("content", "")[:300],
            "path":     c.get("source_path", ""),
        }
        for c in chunks
    ])
# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the WFCF Standards admin assistant.

When the user wants to perform an action, extract whatever params they already mentioned
and emit ONLY a JSON object — nothing else, no explanation.

## Actions and their JSON format:

Add user to a standard:
{"action": "add_user_to_standard", "standard": "<slug or empty>", "tier": "<user|admin or empty>", "user_query": "<name or empty>"}

Add user to a subgroup:
- "add member to subgroup" or "add X to subgroup Y" → action is "add_user_to_subgroup", not "add_user_to_standard".
- If standard is not mentioned, use "" — the system will ask for it.
{"action": "add_user_to_subgroup", "standard": "<slug or empty>", "slug": "<subgroup slug or empty>", "user_query": "<name or empty>"}

Create a standard:
{"action": "create_standard", "name": "<slug or empty>", "display_name": "<display name or empty>"}

Create a subgroup:
{"action": "create_subgroup", "standard": "<slug or empty>", "slug": "<subgroup slug or empty>", "display_name": "<display name or empty>"}

Grant or revoke a subgroup path:
{"action": "grant_revoke_path", "standard": "<slug or empty>", "slug": "<subgroup slug or empty>", "action_type": "<grant|revoke or empty>", "path": "<ADLS path like 'Organic/regulatory' or empty>"}
- "slug" is the subgroup name (e.g. 'reg1'). "path" is the ADLS folder path (e.g. 'Organic/regulatory'). Never confuse them.

Remove user from standard or subgroup:
{"action": "remove_user", "standard": "<slug or empty>", "scope": "<standard|subgroup or empty>", "slug": "<subgroup slug or empty>", "user_query": "<name or empty>"}

## Rules:
- Use "" for any param not mentioned — never guess or assume.
- DO NOT ask the user for anything — the system will collect missing params automatically.
- Emit JSON exactly once and stop. Never repeat it.
- After emitting JSON, do not re-emit it unless the user explicitly asks to retry.
- "ok", "yes", "proceed", "go ahead" are confirmations, not new requests — do not re-emit previous JSON.You said: even if 3 differen aciones?

## For read-only / list requests (NOT actions):
- "list standards" → call list_standards_tool, show as readable list.
- "list members of standard X" or "list members of X" → call list_standard_members_tool(standard="X"). If standard name not mentioned, ask the user for it before calling.
- "list subgroups of X" or "list subgroups" → call list_subgroups_tool. If standard not mentioned, ask the user for it first. This is a tool call, NOT a JSON action.

- "list members of subgroup X in Y" or "who is in subgroup X" → call list_subgroup_members_tool(standard="Y", slug="X"). ALWAYS call the tool.
- "list paths of subgroup X in Y" or "paths for X" → call list_subgroup_paths_tool(standard="Y", slug="X"). ALWAYS call the tool.
- "search documents about X" or "find documents on X" → call search_documents_tool(query="X").
- NEVER answer list queries from memory. NEVER say "there is an issue" .
"""


# ─────────────────────────────────────────────────────────────────────────────
# INTENT ROUTER
# ─────────────────────────────────────────────────────────────────────────────

async def handle_action(parsed: dict) -> WorkflowResult | None:
    action = parsed.get("action")
    with tracer.start_as_current_span(f"workflow.{action}") as span:
        span.set_attribute("action", action)
        span.set_attribute("params", json.dumps(parsed)[:500])

        if action == "add_user_to_standard":
            extracted = {
                "standard":   parsed.get("standard", ""),
                "tier":       parsed.get("tier", ""),
                "user_query": parsed.get("user_query", ""),
            }
            workflow, msg = await add_user_to_standard.build(extracted)

        elif action == "add_user_to_subgroup":
            extracted = {
                "standard":   parsed.get("standard", ""),
                "slug":       parsed.get("slug", ""),
                "user_query": parsed.get("user_query", ""),
            }
            workflow, msg = await add_user_to_subgroup.build(extracted)

        elif action == "create_standard":
            extracted = {
                "name":         parsed.get("name", ""),
                "display_name": parsed.get("display_name", ""),
            }
            workflow, msg = await create_standard.build(extracted)

        elif action == "create_subgroup":
            extracted = {
                "standard":     parsed.get("standard", ""),
                "slug":         parsed.get("slug", ""),
                "display_name": parsed.get("display_name", ""),
            }
            workflow, msg = await create_subgroup.build(extracted)

        elif action == "grant_revoke_path":
            extracted = {
                "standard": parsed.get("standard", ""),
                "slug":     parsed.get("slug", ""),
                "action":   parsed.get("action_type", ""),
                "path":     parsed.get("path", ""),
            }
            workflow, msg = await grant_revoke_path.build(extracted)

        elif action == "remove_user":
            extracted = {
                "standard":   parsed.get("standard", ""),
                "scope":      parsed.get("scope", ""),
                "slug":       parsed.get("slug", ""),
                "user_query": parsed.get("user_query", ""),
            }
            workflow, msg = await remove_user.build(extracted)

        else:
            print(f"Agent: Unknown action '{action}'\n")
            span.set_attribute("result.status", "unknown_action")
            return None

        result: WorkflowResult = await run_workflow(workflow, msg)

        if result.status == "success":
            print(f"\n✓ {result.message}\n")
        elif result.status == "already_done":
            print(f"\nℹ  {result.message}\n")
        elif result.status == "cancelled":
            print(f"\n↩  {result.message}\n")
        else:
            print(f"\n✗ {result.message}\n")

        for w in result.warnings:
            print(f"  ⚠ {w}")

        span.set_attribute("result.status", result.status)

    return result

# new
# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=DefaultAzureCredential(),
    )

    agent = client.as_agent(
        name="WFCFAdmin",
        instructions=SYSTEM_PROMPT,
    )

    # new
    agent_tools = [
        list_standards_tool,
        list_standard_members_tool,
        list_subgroups_tool,
        list_subgroup_members_tool,
        list_subgroup_paths_tool,
        search_documents_tool,
    ]

    print("WFCF Admin Assistant ready. Type 'quit' to exit.\n")

    session = agent.create_session()

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        response = await agent.run(
            user_input,
            session=session,
            tools=agent_tools,
        )
        text = response.text.strip()

        # Try to parse as action JSON
        try:
            clean  = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(clean)

            # new
            if isinstance(parsed, list):
                for item in parsed:
                    if "action" in item:
                        await handle_action(item)
            elif "action" in parsed:
                await handle_action(parsed)
            else:
                print(f"Agent: {text}\n")

        except (json.JSONDecodeError, KeyError):
            # Normal conversational response
            print(f"Agent: {text}\n")


if __name__ == "__main__":
    asyncio.run(main())
