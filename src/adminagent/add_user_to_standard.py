"""workflows/add_user_to_standard.py — server-only.

Flow: SearchUsersExecutor (search + human pick) -> AssignStandardExecutor.
Param collection (standard/tier/user_query) happens upstream in
router_workflow.py's BridgeAddUserStandard before this is ever reached.
"""

from typing import Any
from agent_framework import Executor, WorkflowContext, handler, response_handler
from messages import CollectedParams, UserPickResult, WorkflowResult
from mcp_client import call_mcp
from request_compat import rget
from action_queue import advance_or_finish


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
            request_data={
                "_type": "user_pick",
                "matches": matches,
                "carry": {"standard": p["standard"], "tier": p["tier"]},
            },
            response_type=str,
        )

    @response_handler
    async def handle_pick(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        matches = rget(original_request, "matches", [])
        carry   = rget(original_request, "carry", {})
        chosen_oid = chosen_name = chosen_email = None

        if response.strip().isdigit():
            idx = int(response.strip()) - 1
            if 0 <= idx < len(matches):
                chosen_oid   = matches[idx]["oid"]
                chosen_name  = matches[idx]["name"]
                chosen_email = matches[idx]["email"]
            else:
                await ctx.yield_output(WorkflowResult(status="failed", message=f"Pick between 1 and {len(matches)}, or type 'cancel'."))
                return
        else:
            for m in matches:
                if response.strip() in (m["oid"], m["email"]):
                    chosen_oid, chosen_name, chosen_email = m["oid"], m["name"], m["email"]
                    break

        if not chosen_oid:
            await ctx.yield_output(WorkflowResult(status="failed", message="Not found. Enter the number shown, or type 'cancel'."))
            return

        await ctx.send_message(UserPickResult(
            oid=chosen_oid, name=chosen_name, email=chosen_email,
            carry=carry,
        ))


class AssignStandardExecutor(Executor):
    def __init__(self):
        super().__init__(id="assign_standard")

    @handler
    async def handle(self, request: Any, ctx: WorkflowContext) -> None:
        c     = rget(request, "carry", {})
        oid   = rget(request, "oid")
        name  = rget(request, "name")
        email = rget(request, "email")

        result = await call_mcp("assign_user_to_standard", {
            "standard": c["standard"],
            "tier":     c["tier"],
            "user_oid": oid,
        })

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Assignment failed: {result['error']}"))
            return

        if result.get("already_member"):
            await advance_or_finish(ctx, WorkflowResult(
                status="already_done",
                message=f"{name} ({email}) is already a {c['tier']} of '{c['standard']}' — no change.",
            ))
            return

        await advance_or_finish(ctx, WorkflowResult(
            status="success",
            message=f"✓ Added {name} ({email}) to '{c['standard']}' as {c['tier']}.",
        ))