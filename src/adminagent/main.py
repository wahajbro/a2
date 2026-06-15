"""
main.py — WFCF Admin Agent entry point

CLI mode  (python main.py):         input() loop via workflow_runner.py  ← unchanged
Server mode (python main.py serve): WorkflowAgent + ResponsesHostServer  ← new

HOW THE SERVER MODE WORKS
─────────────────────────
The outer LLM agent detects intent and emits a JSON action dict as a tool call.
We register each workflow as a WorkflowAgent tool on the outer agent.
When a workflow is invoked:
  1. Its start executor receives list[ChatMessage] (WorkflowAgent normalises input).
  2. Every ctx.request_info() pause surfaces as a function call named
     WorkflowAgent.REQUEST_INFO_FUNCTION_NAME in the Responses stream.
  3. The client shows the prompt, collects the human answer, sends it back
     as a function_call_output — WorkflowAgent resumes the workflow.
  4. Azure manages session state between HTTP turns automatically.

No FastAPI. No workflow_runner_server. No custom HTTP server.
workflow_runner.py is used only by CLI mode and is completely unchanged.
"""

import asyncio
import json
import os
import sys
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from agent_framework_foundry import FoundryChatClient

from messages import WorkflowResult
from mcp_client import call_mcp
from workflow_runner import run_workflow   # CLI only

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
# TELEMETRY
# ─────────────────────────────────────────────────────────────────────────────
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

configure_azure_monitor(
    connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
)
tracer = trace.get_tracer(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# AGENT TOOLS — read-only list queries (same in both modes)
# ─────────────────────────────────────────────────────────────────────────────

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

async def list_standard_members_tool(standard: str) -> str:
    """List current members of a standard. standard must be a valid slug like 'organic'."""
    if not standard or len(standard) < 2:
        return "Error: Please provide a valid standard name (e.g. 'organic')."
    result = await call_mcp("list_standard_members", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    return json.dumps([
        {"name": m.get("display_name", ""), "email": m.get("email", ""), "tier": m.get("tier", "")}
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
        {"name": m.get("display_name", ""), "email": m.get("email", "")}
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
            "title":   c.get("title", ""),
            "section": c.get("section", ""),
            "content": c.get("content", "")[:300],
            "path":    c.get("source_path", ""),
        }
        for c in chunks
    ])

AGENT_TOOLS = [
    list_standards_tool,
    list_standard_members_tool,
    list_subgroups_tool,
    list_subgroup_members_tool,
    list_subgroup_paths_tool,
    search_documents_tool,
]

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
- "slug" is the subgroup name (e.g. 'reg1'). "path" is the ADLS folder path. Never confuse them.

Remove user from standard or subgroup:
{"action": "remove_user", "standard": "<slug or empty>", "scope": "<standard|subgroup or empty>", "slug": "<subgroup slug or empty>", "user_query": "<name or empty>"}

## Rules:
- Use "" for any param not mentioned — never guess or assume.
- DO NOT ask the user for anything — the system will collect missing params automatically.
- Emit JSON exactly once and stop. Never repeat it.
- After emitting JSON, do not re-emit it unless the user explicitly asks to retry.
- "ok", "yes", "proceed", "go ahead" are confirmations, not new requests — do not re-emit previous JSON.

## For read-only / list requests (NOT actions):
- "list standards" → call list_standards_tool, show as readable list.
- "list members of standard X" → call list_standard_members_tool(standard="X").
- "list subgroups of X" → call list_subgroups_tool.
- "list members of subgroup X in Y" → call list_subgroup_members_tool(standard="Y", slug="X").
- "list paths of subgroup X in Y" → call list_subgroup_paths_tool(standard="Y", slug="X").
- "search documents about X" → call search_documents_tool(query="X").
- NEVER answer list queries from memory. NEVER say "there is an issue".
"""

# ─────────────────────────────────────────────────────────────────────────────
# SHARED: build a workflow from a parsed action dict
# ─────────────────────────────────────────────────────────────────────────────

async def build_workflow(parsed: dict):
    """Return (workflow, initial_message) for a parsed action dict."""
    action = parsed.get("action")

    if action == "add_user_to_standard":
        return await add_user_to_standard.build({
            "standard":   parsed.get("standard", ""),
            "tier":       parsed.get("tier", ""),
            "user_query": parsed.get("user_query", ""),
        })
    elif action == "add_user_to_subgroup":
        return await add_user_to_subgroup.build({
            "standard":   parsed.get("standard", ""),
            "slug":       parsed.get("slug", ""),
            "user_query": parsed.get("user_query", ""),
        })
    elif action == "create_standard":
        return await create_standard.build({
            "name":         parsed.get("name", ""),
            "display_name": parsed.get("display_name", ""),
        })
    elif action == "create_subgroup":
        return await create_subgroup.build({
            "standard":     parsed.get("standard", ""),
            "slug":         parsed.get("slug", ""),
            "display_name": parsed.get("display_name", ""),
        })
    elif action == "grant_revoke_path":
        return await grant_revoke_path.build({
            "standard": parsed.get("standard", ""),
            "slug":     parsed.get("slug", ""),
            "action":   parsed.get("action_type", ""),
            "path":     parsed.get("path", ""),
        })
    elif action == "remove_user":
        return await remove_user.build({
            "standard":   parsed.get("standard", ""),
            "scope":      parsed.get("scope", ""),
            "slug":       parsed.get("slug", ""),
            "user_query": parsed.get("user_query", ""),
        })

    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# CLI MODE — original behaviour, completely unchanged
# ─────────────────────────────────────────────────────────────────────────────

async def handle_action_cli(parsed: dict) -> WorkflowResult | None:
    action = parsed.get("action")
    with tracer.start_as_current_span(f"workflow.{action}") as span:
        span.set_attribute("action", action)
        span.set_attribute("params", json.dumps(parsed)[:500])

        workflow, msg = await build_workflow(parsed)
        if workflow is None:
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


async def run_cli():
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=DefaultAzureCredential(),
    )
    agent = client.as_agent(name="WFCFAdmin", instructions=SYSTEM_PROMPT)

    print("WFCF Admin Assistant ready. Type 'quit' to exit.\n")
    session = agent.create_session()

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        response = await agent.run(user_input, session=session, tools=AGENT_TOOLS)
        text = response.text.strip()

        try:
            clean  = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(clean)

            if isinstance(parsed, list):
                for item in parsed:
                    if "action" in item:
                        await handle_action_cli(item)
            elif "action" in parsed:
                await handle_action_cli(parsed)
            else:
                print(f"Agent: {text}\n")

        except (json.JSONDecodeError, KeyError):
            print(f"Agent: {text}\n")


# ─────────────────────────────────────────────────────────────────────────────
# SERVER MODE — WorkflowAgent + ResponsesHostServer
#
# Architecture:
#   Each workflow is wrapped as a WorkflowAgent and registered as a tool
#   on the outer LLM agent. The outer agent detects intent and calls the
#   right WorkflowAgent tool. WorkflowAgent surfaces request_info pauses
#   as function calls (WorkflowAgent.REQUEST_INFO_FUNCTION_NAME) in the
#   Responses protocol stream. The client sends answers back as
#   function_call_output items. Azure manages session state between turns.
#
# Why WorkflowAgent as a tool (not ResponsesHostServer(workflow_agent))?
#   Your architecture has ONE entry point that handles MULTIPLE possible
#   workflows depending on intent. The outer agent is the router.
#   Wrapping each workflow as a tool lets the outer agent pick the right
#   one, and WorkflowAgent handles the pause/resume protocol for each.
# ─────────────────────────────────────────────────────────────────────────────

async def run_server():
    from agent_framework import WorkflowAgent
    from agent_framework_foundry_hosting import ResponsesHostServer

    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=DefaultAzureCredential(),
    )

    # Pre-build WorkflowAgents for each action at startup.
    # Each workflow's start executor now accepts list[ChatMessage]
    # (via the handle_messages handler added to ParamCollectorExecutor).
    # The extracted dict is baked into self._initial at build time,
    # so WorkflowAgent can pass list[ChatMessage] and it just works.
    #
    # We build "empty" workflows (all params empty) as the tool definitions.
    # The actual extracted params come from the LLM JSON, which the outer
    # agent passes as the tool call arguments. The WorkflowAgent receives
    # those as the user message content converted to list[ChatMessage].
    #
    # Simpler and more reliable: let the outer agent call build_workflow()
    # dynamically per turn using a tool that wraps the workflow execution.

    async def run_add_user_to_standard(standard: str = "", tier: str = "", user_query: str = "") -> str:
        """Add a user to a WFCF standard. Collects any missing params interactively."""
        workflow, msg = await add_user_to_standard.build(
            {"standard": standard, "tier": tier, "user_query": user_query}
        )
        wa = workflow.as_agent(name="add_user_to_standard")
        return wa  # WorkflowAgent — MAF handles request_info pauses

    async def run_add_user_to_subgroup(standard: str = "", slug: str = "", user_query: str = "") -> str:
        """Add a user to a subgroup. Collects any missing params interactively."""
        workflow, msg = await add_user_to_subgroup.build(
            {"standard": standard, "slug": slug, "user_query": user_query}
        )
        wa = workflow.as_agent(name="add_user_to_subgroup")
        return wa

    async def run_create_standard(name: str = "", display_name: str = "") -> str:
        """Create a new WFCF standard. Confirms before creating."""
        workflow, msg = await create_standard.build(
            {"name": name, "display_name": display_name}
        )
        wa = workflow.as_agent(name="create_standard")
        return wa

    async def run_create_subgroup(standard: str = "", slug: str = "", display_name: str = "") -> str:
        """Create a new subgroup under a standard."""
        workflow, msg = await create_subgroup.build(
            {"standard": standard, "slug": slug, "display_name": display_name}
        )
        wa = workflow.as_agent(name="create_subgroup")
        return wa

    async def run_grant_revoke_path(standard: str = "", slug: str = "", action_type: str = "", path: str = "") -> str:
        """Grant or revoke an ADLS path for a subgroup."""
        workflow, msg = await grant_revoke_path.build(
            {"standard": standard, "slug": slug, "action": action_type, "path": path}
        )
        wa = workflow.as_agent(name="grant_revoke_path")
        return wa

    async def run_remove_user(standard: str = "", scope: str = "", slug: str = "", user_query: str = "") -> str:
        """Remove a user from a standard or subgroup. Confirms before removing."""
        workflow, msg = await remove_user.build(
            {"standard": standard, "scope": scope, "slug": slug, "user_query": user_query}
        )
        wa = workflow.as_agent(name="remove_user")
        return wa

    # Outer agent: intent detection + list queries + workflow dispatch
    outer_agent = client.as_agent(
        name="WFCFAdmin",
        instructions=SYSTEM_PROMPT,
        tools=[
            # read-only list tools
            *AGENT_TOOLS,
            # workflow tools — each returns a WorkflowAgent
            # MAF's tool invocation handles WorkflowAgent tools natively:
            # request_info pauses surface as function calls to the client
            run_add_user_to_standard,
            run_add_user_to_subgroup,
            run_create_standard,
            run_create_subgroup,
            run_grant_revoke_path,
            run_remove_user,
        ],
    )

    # ResponsesHostServer exposes the outer agent on port 8088
    # WorkflowAgent request_info pauses flow through the Responses protocol automatically
    server = ResponsesHostServer(outer_agent)
    await server.run_async()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # python main.py        → CLI (original, unchanged)
    # python main.py serve  → server for Azure Foundry deployment
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        asyncio.run(run_server())
    else:
        asyncio.run(run_cli())