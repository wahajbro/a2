"""workflows/add_user_to_standard.py

Flow:
  ParamCollectorExecutor  → collects: standard (validated vs real list), tier, user_query
  SearchUsersExecutor     → calls search_users MCP, pauses for human pick
  AssignStandardExecutor  → calls assign_user_to_standard MCP
"""

#from __future__ import annotations
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler
from messages import (
    CollectedParams, UserPickRequest, UserPickResult,
    WorkflowResult, ParamSpec, ConfirmRequest,
)
from param_collector import ParamCollectorExecutor
from mcp_client import call_mcp


# ── Executor 1: Search ────────────────────────────────────────────────────────

class SearchUsersExecutor(Executor):
    def __init__(self):
        super().__init__(id="search_users_std")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        result = await call_mcp("search_users", {"query": p["user_query"]})
        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Search failed: {result['error']}"))
            return

        users = result.get("users", [])
        if not users:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"No user found matching '{p['user_query']}'. Try a different name.",
            ))
            return

        matches = [
            {
                "name":  u.get("displayName") or u.get("display_name") or "Unknown",
                "email": u.get("mail") or u.get("userPrincipalName") or u.get("email") or "",
                "oid":   u.get("id") or u.get("oid") or "",
            }
            for u in users[:20]
        ]

        await ctx.request_info(
            request_data=UserPickRequest(
                matches=matches,
                carry={"standard": p["standard"], "tier": p["tier"]},
            ),
            response_type=str,
        )

    @response_handler
    async def handle_pick(self, original_request: UserPickRequest, response: str, ctx: WorkflowContext) -> None:
        matches = original_request.matches
        chosen_oid = chosen_name = chosen_email = None

        if response.strip().isdigit():
            idx = int(response.strip()) - 1
            if 0 <= idx < len(matches):
                chosen_oid   = matches[idx]["oid"]
                chosen_name  = matches[idx]["name"]
                chosen_email = matches[idx]["email"]
            else:
                await ctx.yield_output(WorkflowResult(status="failed", message=f"Pick between 1 and {len(matches)}."))
                return
        else:
            for m in matches:
                if response.strip() in (m["oid"], m["email"]):
                    chosen_oid, chosen_name, chosen_email = m["oid"], m["name"], m["email"]
                    break

        if not chosen_oid:
            await ctx.yield_output(WorkflowResult(status="failed", message="Not found. Enter the number shown."))
            return

        await ctx.send_message(UserPickResult(
            oid=chosen_oid, name=chosen_name, email=chosen_email,
            carry=original_request.carry,
        ))


# ── Executor 2: Assign ────────────────────────────────────────────────────────

class AssignStandardExecutor(Executor):
    def __init__(self):
        super().__init__(id="assign_standard")

    @handler
    async def handle(self, request: UserPickResult, ctx: WorkflowContext) -> None:
        c = request.carry
        result = await call_mcp("assign_user_to_standard", {
            "standard": c["standard"],
            "tier":     c["tier"],
            "user_oid": request.oid,
        })

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Assignment failed: {result['error']}"))
            return

        if result.get("already_member"):
            await ctx.yield_output(WorkflowResult(
                status="already_done",
                message=f"{request.name} ({request.email}) is already a {c['tier']} of '{c['standard']}' — no change.",
            ))
            return

        await ctx.yield_output(WorkflowResult(
            status="success",
            message=f"✓ Added {request.name} ({request.email}) to '{c['standard']}' as {c['tier']}.",
        ))


# ── Builder ───────────────────────────────────────────────────────────────────

async def build(extracted: dict) -> tuple:
    """Fetch real standards, build param specs, return (workflow, initial_message)."""
    std_result = await call_mcp("list_standards", {})
    standards  = std_result.get("standards", []) if "error" not in std_result else []
    std_slugs  = [s["name"] for s in standards]
    std_prompt = "Which standard?\n" + "\n".join(
        f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}"
        for i, s in enumerate(standards)
    )

    specs = [
        ParamSpec("standard",    std_prompt,                         choices=std_slugs),
        ParamSpec("tier",        "Add as 'user' or 'admin'?",        choices=["user", "admin"]),
        ParamSpec("user_query",  "Name of the person to add?",       choices=[]),
    ]

    collector = ParamCollectorExecutor(specs=specs, initial=extracted)
    search    = SearchUsersExecutor()
    assign    = AssignStandardExecutor()

    workflow = (
        WorkflowBuilder(start_executor=collector)
        .add_edge(collector, search)
        .add_edge(search, assign)
        .build()
    )
    # ParamCollectorExecutor.handle receives the initial dict as message
    return workflow, extracted
