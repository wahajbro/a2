"""workflows/grant_revoke_path.py — server-only.

v22:
  - Revoke now validates the LLM-extracted path against the subgroup's
    actual granted paths before trusting it; falls back to the numbered
    picker if it doesn't match (fixes 'Organic/interpretive' getting
    silently passed to MCP as just 'interpretive').
  - ExecutePathExecutor's success/already-granted outputs now go through
    advance_or_finish() instead of a bare yield_output, so a queued
    follow-up action (from a multi-action message) can continue in the
    same turn. Failure outputs stay as plain yield_output — chaining
    after a failure isn't safe to assume.
"""

from typing import Any
from dataclasses import dataclass
from agent_framework import Executor, WorkflowContext, handler, response_handler
from messages import CollectedParams, WorkflowResult
from mcp_client import call_mcp
from request_compat import rget
from action_queue import advance_or_finish


@dataclass
class PathPickedRequest:
    standard: str
    slug: str
    action: str
    path: str


class PathPickExecutor(Executor):
    def __init__(self):
        super().__init__(id="path_pick")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        action = p["action"]

        if action == "revoke":
            result = await call_mcp("list_subgroup_paths", {"standard": p["standard"], "slug": p["slug"]})
            paths  = result.get("paths", []) if "error" not in result else []
            if not paths:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message=f"Subgroup '{p['slug']}' has no granted paths to revoke.",
                ))
                return

            given = p.get("path", "").strip()
            if given and given in paths:
                # LLM-extracted path matches a real granted path — trust it.
                await ctx.send_message(PathPickedRequest(
                    standard=p["standard"], slug=p["slug"], action="revoke", path=given,
                ))
                return

            # Either no path given, or what the LLM extracted doesn't match
            # anything real (e.g. got truncated to 'interpretive' instead of
            # 'Organic/interpretive') — don't guess, show the real list.
            numbered = "\n".join(f"  {i+1}. {path}" for i, path in enumerate(paths))
            prompt = f"Current paths for '{p['slug']}':\n{numbered}\nEnter number or path to revoke (or 'cancel'):"
            await ctx.request_info(
                request_data={"_type": "param_ask", "field": "path", "prompt": prompt,
                              "choices": paths, "current": p, "remaining_specs": []},
                response_type=str,
            )
        else:
            if p.get("path"):
                # Nothing to validate a grant against — a granted path can
                # legitimately be new, so trust the LLM-extracted value as-is.
                await ctx.send_message(PathPickedRequest(
                    standard=p["standard"], slug=p["slug"], action=p["action"], path=p["path"],
                ))
                return
            prompt = (
                f"Enter the ADLS path to grant to '{p['slug']}'\n"
                f"(e.g. '{p['standard'].capitalize()}/policies' or '{p['standard'].capitalize()}/policies/spec.pdf'):"
            )
            await ctx.request_info(
                request_data={"_type": "param_ask", "field": "path", "prompt": prompt,
                              "choices": [], "current": p, "remaining_specs": []},
                response_type=str,
            )

    @response_handler
    async def handle_path(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        p      = rget(original_request, "current", {})
        action = p["action"]
        value  = response.strip()
        paths  = rget(original_request, "choices", [])

        if action == "revoke" and paths:
            if value.isdigit():
                idx = int(value) - 1
                if 0 <= idx < len(paths):
                    value = paths[idx]
                else:
                    await ctx.yield_output(WorkflowResult(status="failed", message=f"Pick between 1 and {len(paths)}, or type 'cancel'."))
                    return
            elif value not in paths:
                await ctx.yield_output(WorkflowResult(status="failed", message=f"'{value}' is not in the current path list, or type 'cancel'."))
                return

        if not value:
            await ctx.yield_output(WorkflowResult(status="failed", message="Path cannot be empty."))
            return

        await ctx.send_message(PathPickedRequest(
            standard=p["standard"], slug=p["slug"], action=action, path=value,
        ))


class ExecutePathExecutor(Executor):
    def __init__(self):
        super().__init__(id="execute_path")

    @handler
    async def handle(self, request: Any, ctx: WorkflowContext) -> None:
        standard = rget(request, "standard")
        slug     = rget(request, "slug")
        action   = rget(request, "action")
        path     = rget(request, "path")

        tool   = "grant_subgroup_path" if action == "grant" else "revoke_subgroup_path"
        result = await call_mcp(tool, {"standard": standard, "slug": slug, "path": path})

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Failed: {result['error']}"))
            return

        if result.get("already_granted"):
            await advance_or_finish(ctx, WorkflowResult(
                status="already_done",
                message=f"Path '{path}' was already granted to '{slug}' — no change.",
            ))
            return

        verb = "Granted" if action == "grant" else "Revoked"
        await advance_or_finish(ctx, WorkflowResult(
            status="success",
            message=f"✓ {verb} path '{path}' for subgroup '{slug}' under '{standard}'.",
            warnings=result.get("warnings", []),
        ))