"""workflows/remove_user.py — server-only."""

from typing import Any
from dataclasses import dataclass
from agent_framework import Executor, WorkflowContext, handler, response_handler
from messages import CollectedParams, UserPickResult, WorkflowResult
from mcp_client import call_mcp
from request_compat import rget
from action_queue import advance_or_finish


@dataclass
class RemoveRequest:
    standard: str
    scope: str
    slug: str
    user_oid: str
    user_name: str
    user_email: str


class SearchUsersExecutor(Executor):
    def __init__(self):
        super().__init__(id="search_users_remove")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        query = p["user_query"].lower()
        if p.get("scope") == "subgroup" and p.get("slug"):
            members_result = await call_mcp("list_subgroup_members", {"standard": p["standard"], "slug": p["slug"]})
        else:
            members_result = await call_mcp("list_standard_members", {"standard": p["standard"]})
        all_members = members_result.get("members", [])

        users = [m for m in all_members if query in (m.get("display_name") or "").lower()
                 or query in (m.get("email") or "").lower()]
        if not users:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"No user found matching '{p['user_query']}'. Try a different name.",
            ))
            return

        matches = [
            {
                "name":  u.get("display_name") or "Unknown",
                "email": u.get("email") or "",
                "oid":   u.get("oid") or "",
            }
            for u in users[:20]
        ]

        await ctx.request_info(
            request_data={
                "_type": "user_pick",
                "matches": matches,
                "carry": {"standard": p["standard"], "scope": p["scope"], "slug": p.get("slug", "")},
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


class ConfirmRemoveExecutor(Executor):
    def __init__(self):
        super().__init__(id="confirm_remove")

    @handler
    async def handle(self, request: Any, ctx: WorkflowContext) -> None:
        c     = rget(request, "carry", {})
        oid   = rget(request, "oid")
        name  = rget(request, "name")
        email = rget(request, "email")

        scope = c["scope"]
        where = f"subgroup '{c['slug']}' under '{c['standard']}'" if scope == "subgroup" else f"standard '{c['standard']}'"
        note  = " (also removes from all subgroups)" if scope == "standard" else ""
        await ctx.request_info(
            request_data={
                "_type": "confirm",
                "message": (
                    f"Remove {name} ({email}) from {where}{note}?\n"
                    f"Type 'yes' to confirm or 'no' to cancel:"
                ),
                "choices": ["yes", "no"],
                "carry": {
                    "standard":   c["standard"],
                    "scope":      scope,
                    "slug":       c.get("slug", ""),
                    "user_oid":   oid,
                    "user_name":  name,
                    "user_email": email,
                },
            },
            response_type=str,
        )

    @response_handler
    async def handle_confirm(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        c = rget(original_request, "carry", {})
        if response.strip().lower() not in ("yes", "y", "confirm"):
            await ctx.yield_output(WorkflowResult(status="cancelled", message="Removal cancelled."))
            return
        await ctx.send_message(RemoveRequest(
            standard=c["standard"], scope=c["scope"], slug=c["slug"],
            user_oid=c["user_oid"], user_name=c["user_name"], user_email=c["user_email"],
        ))


class RemoveExecutor(Executor):
    def __init__(self):
        super().__init__(id="remove_user")

    @handler
    async def handle(self, request: Any, ctx: WorkflowContext) -> None:
        standard   = rget(request, "standard")
        scope      = rget(request, "scope")
        slug       = rget(request, "slug")
        user_oid   = rget(request, "user_oid")
        user_name  = rget(request, "user_name")
        user_email = rget(request, "user_email")

        if scope == "subgroup":
            result = await call_mcp("remove_user_from_subgroup", {
                "standard": standard, "slug": slug, "user_oid": user_oid,
            })
            where = f"subgroup '{slug}'"
        else:
            result = await call_mcp("remove_user_from_standard", {
                "standard": standard, "user_oid": user_oid,
            })
            where = f"standard '{standard}'"

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Removal failed: {result['error']}"))
            return

        extra = ""
        if scope == "standard":
            n = result.get("subgroups_processed", 0)
            extra = f" (also removed from {n} subgroup(s))"

        await advance_or_finish(ctx, WorkflowResult(
            status="success",
            message=f"✓ Removed {user_name} ({user_email}) from {where}{extra}.",
            warnings=result.get("warnings", []),
        ))