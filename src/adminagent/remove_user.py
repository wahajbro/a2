"""workflows/remove_user.py

Flow:
  ParamCollectorExecutor  → collects: standard, scope (standard/subgroup), slug (if subgroup), user_query
  SearchUsersExecutor     → search + human pick
  ConfirmExecutor         → shows who/what will be removed, asks yes/no
  RemoveExecutor          → calls remove_user_from_standard or remove_user_from_subgroup MCP
"""

# from __future__ import annotations
from dataclasses import dataclass
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler
from messages import (
    CollectedParams, UserPickRequest, UserPickResult,
    ConfirmRequest, WorkflowResult, ParamSpec,
)
from param_collector import ParamCollectorExecutor
from mcp_client import call_mcp


@dataclass
class RemoveRequest:
    standard: str
    scope: str       # "standard" | "subgroup"
    slug: str        # subgroup slug — empty if scope == "standard"
    user_oid: str
    user_name: str
    user_email: str


# ── Executor 1: Search ────────────────────────────────────────────────────────

class SearchUsersExecutor(Executor):
    def __init__(self):
        super().__init__(id="search_users_remove")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        #result = await call_mcp("search_users", {"query": p["user_query"]})
        # new
        query = p["user_query"].lower()
        if p.get("scope") == "subgroup" and p.get("slug"):
            print(f"DEBUG scope={p.get('scope')} slug={p.get('slug')} standard={p.get('standard')}")
            members_result = await call_mcp("list_subgroup_members", {"standard": p["standard"], "slug": p["slug"]})
        else:
            members_result = await call_mcp("list_standard_members", {"standard": p["standard"]})
        all_members = members_result.get("members", [])
        # new
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
                # new
                "name":  u.get("display_name") or "Unknown",
                "email": u.get("email") or "",
                "oid":   u.get("oid") or "",
            }
            for u in users[:20]
        ]

        await ctx.request_info(
            request_data=UserPickRequest(
                matches=matches,
                carry={"standard": p["standard"], "scope": p["scope"], "slug": p.get("slug", "")},
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


# ── Executor 2: Confirm ───────────────────────────────────────────────────────

class ConfirmRemoveExecutor(Executor):
    def __init__(self):
        super().__init__(id="confirm_remove")

    @handler
    async def handle(self, request: UserPickResult, ctx: WorkflowContext) -> None:
        c     = request.carry
        scope = c["scope"]
        where = f"subgroup '{c['slug']}' under '{c['standard']}'" if scope == "subgroup" else f"standard '{c['standard']}'"
        note  = " (also removes from all subgroups)" if scope == "standard" else ""
        await ctx.request_info(
            request_data=ConfirmRequest(
                message=(
                    f"Remove {request.name} ({request.email}) from {where}{note}?\n"
                    f"Type 'yes' to confirm or 'no' to cancel:"
                ),
                carry={
                    "standard":   c["standard"],
                    "scope":      scope,
                    "slug":       c.get("slug", ""),
                    "user_oid":   request.oid,
                    "user_name":  request.name,
                    "user_email": request.email,
                },
            ),
            response_type=str,
        )

    @response_handler
    async def handle_confirm(self, original_request: ConfirmRequest, response: str, ctx: WorkflowContext) -> None:
        if response.strip().lower() not in ("yes", "y"):
            await ctx.yield_output(WorkflowResult(status="cancelled", message="Removal cancelled."))
            return
        c = original_request.carry
        await ctx.send_message(RemoveRequest(
            standard=c["standard"], scope=c["scope"], slug=c["slug"],
            user_oid=c["user_oid"], user_name=c["user_name"], user_email=c["user_email"],
        ))


# ── Executor 3: Remove ────────────────────────────────────────────────────────

class RemoveExecutor(Executor):
    def __init__(self):
        super().__init__(id="remove_user")

    @handler
    async def handle(self, request: RemoveRequest, ctx: WorkflowContext) -> None:
        if request.scope == "subgroup":
            result = await call_mcp("remove_user_from_subgroup", {
                "standard": request.standard,
                "slug":     request.slug,
                "user_oid": request.user_oid,
            })
            where = f"subgroup '{request.slug}'"
        else:
            result = await call_mcp("remove_user_from_standard", {
                "standard": request.standard,
                "user_oid": request.user_oid,
            })
            where = f"standard '{request.standard}'"

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Removal failed: {result['error']}"))
            return

        extra = ""
        if request.scope == "standard":
            n = result.get("subgroups_processed", 0)
            extra = f" (also removed from {n} subgroup(s))"

        await ctx.yield_output(WorkflowResult(
            status="success",
            message=f"✓ Removed {request.user_name} ({request.user_email}) from {where}{extra}.",
            warnings=result.get("warnings", []),
        ))


# ── Builder ───────────────────────────────────────────────────────────────────

async def build(extracted: dict) -> tuple:
    std_result = await call_mcp("list_standards", {})
    standards  = std_result.get("standards", []) if "error" not in std_result else []
    std_slugs  = [s["name"] for s in standards]
    std_prompt = "Which standard?\n" + "\n".join(
        f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}"
        for i, s in enumerate(standards)
    )

    known_std   = extracted.get("standard", "").strip()
    known_scope = extracted.get("scope", "").strip()
    sg_slugs    = []
    sg_prompt   = "Which subgroup slug?"

    if known_std and known_std in std_slugs and known_scope == "subgroup":
        sg_result = await call_mcp("list_subgroups", {"standard": known_std})
        sgs       = sg_result.get("subgroups", []) if "error" not in sg_result else []
        sg_slugs  = [s["slug"] for s in sgs]
        sg_prompt = "Which subgroup?\n" + "\n".join(
            f"  {i+1}. {s['slug']} — {s.get('display_name', s['slug'])}"
            for i, s in enumerate(sgs)
        )

    # new
    specs = [
        ParamSpec("standard",   std_prompt,                               choices=std_slugs),
        ParamSpec("scope",      "Remove from 'standard' or 'subgroup'?",  choices=["standard", "subgroup"]),
        ParamSpec("slug",       sg_prompt,                                choices=sg_slugs),
        ParamSpec("user_query", "Name of the person to remove?",          choices=[]),
    ]

    collector = ParamCollectorExecutor(specs=specs, initial=extracted)
    search    = SearchUsersExecutor()
    confirm   = ConfirmRemoveExecutor()
    remove    = RemoveExecutor()

    workflow = (
        WorkflowBuilder(start_executor=collector)
        .add_edge(collector, search)
        .add_edge(search, confirm)
        .add_edge(confirm, remove)
        .build()
    )
    return workflow, extracted
