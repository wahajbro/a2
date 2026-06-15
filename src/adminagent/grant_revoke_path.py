"""workflows/grant_revoke_path.py

Flow:
  ParamCollectorExecutor  → collects: standard, slug, action (grant/revoke)
  PathPickExecutor        → fetches existing paths (for revoke) or asks for new path (for grant)
                            pauses for human to enter/pick path
  ExecutePathExecutor     → calls grant_subgroup_path or revoke_subgroup_path MCP
"""

#from __future__ import annotations
from dataclasses import dataclass
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler
from messages import CollectedParams, ConfirmRequest, WorkflowResult, ParamSpec, ParamAskRequest
from param_collector import ParamCollectorExecutor
from mcp_client import call_mcp


@dataclass
class PathPickedRequest:
    standard: str
    slug: str
    action: str   # "grant" | "revoke"
    path: str


# ── Executor 1: PathPickExecutor ──────────────────────────────────────────────

class PathPickExecutor(Executor):
    def __init__(self):
        super().__init__(id="path_pick")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        action = p["action"]
        if p.get("path"):
            await ctx.send_message(PathPickedRequest(
                standard=p["standard"], slug=p["slug"], action=p["action"], path=p["path"],
            ))
            return

        if action == "revoke":
            # Show existing granted paths so user can pick one to revoke
            result = await call_mcp("list_subgroup_paths", {"standard": p["standard"], "slug": p["slug"]})
            paths  = result.get("paths", []) if "error" not in result else []
            if not paths:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message=f"Subgroup '{p['slug']}' has no granted paths to revoke.",
                ))
                return
            numbered = "\n".join(f"  {i+1}. {path}" for i, path in enumerate(paths))
            prompt = f"Current paths for '{p['slug']}':\n{numbered}\nEnter number or path to revoke:"
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field="path", prompt=prompt,
                    choices=paths, current=p, remaining_specs=[],
                ),
                response_type=str,
            )
        else:
            # Grant — ask for free-text path
            prompt = (
                f"Enter the ADLS path to grant to '{p['slug']}'\n"
                f"(e.g. '{p['standard'].capitalize()}/policies' or '{p['standard'].capitalize()}/policies/spec.pdf'):"
            )
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field="path", prompt=prompt,
                    choices=[], current=p, remaining_specs=[],
                ),
                response_type=str,
            )

    @response_handler
    async def handle_path(self, original_request: ParamAskRequest, response: str, ctx: WorkflowContext) -> None:
        p      = original_request.current
        action = p["action"]
        value  = response.strip()
        paths  = original_request.choices  # non-empty only for revoke

        if action == "revoke" and paths:
            if value.isdigit():
                idx = int(value) - 1
                if 0 <= idx < len(paths):
                    value = paths[idx]
                else:
                    await ctx.yield_output(WorkflowResult(status="failed", message=f"Pick between 1 and {len(paths)}."))
                    return
            elif value not in paths:
                await ctx.yield_output(WorkflowResult(status="failed", message=f"'{value}' is not in the current path list."))
                return

        if not value:
            await ctx.yield_output(WorkflowResult(status="failed", message="Path cannot be empty."))
            return

        await ctx.send_message(PathPickedRequest(
            standard=p["standard"], slug=p["slug"], action=action, path=value,
        ))


# ── Executor 2: ExecutePathExecutor ──────────────────────────────────────────

class ExecutePathExecutor(Executor):
    def __init__(self):
        super().__init__(id="execute_path")

    @handler
    async def handle(self, request: PathPickedRequest, ctx: WorkflowContext) -> None:
        tool   = "grant_subgroup_path" if request.action == "grant" else "revoke_subgroup_path"
        result = await call_mcp(tool, {
            "standard": request.standard,
            "slug":     request.slug,
            "path":     request.path,
        })

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Failed: {result['error']}"))
            return

        if result.get("already_granted"):
            await ctx.yield_output(WorkflowResult(
                status="already_done",
                message=f"Path '{request.path}' was already granted to '{request.slug}' — no change.",
            ))
            return

        verb = "Granted" if request.action == "grant" else "Revoked"
        await ctx.yield_output(WorkflowResult(
            status="success",
            message=f"✓ {verb} path '{request.path}' for subgroup '{request.slug}' under '{request.standard}'.",
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

    known_std = extracted.get("standard", "").strip()
    sg_slugs  = []
    sg_prompt = "Which subgroup slug?"
    if known_std and known_std in std_slugs:
        sg_result = await call_mcp("list_subgroups", {"standard": known_std})
        sgs       = sg_result.get("subgroups", []) if "error" not in sg_result else []
        sg_slugs  = [s["slug"] for s in sgs]
        sg_prompt = "Which subgroup?\n" + "\n".join(
            f"  {i+1}. {s['slug']} — {s.get('display_name', s['slug'])}"
            for i, s in enumerate(sgs)
        )

    specs = [
        ParamSpec("standard", std_prompt,                           choices=std_slugs),
        ParamSpec("slug",     sg_prompt,                            choices=sg_slugs),
        ParamSpec("action",   "Grant or revoke a path?",            choices=["grant", "revoke"]),
    ]

    collector = ParamCollectorExecutor(specs=specs, initial=extracted)
    path_pick = PathPickExecutor()
    execute   = ExecutePathExecutor()

    workflow = (
        WorkflowBuilder(start_executor=collector)
        .add_edge(collector, path_pick)
        .add_edge(path_pick, execute)
        .build()
    )
    return workflow, extracted
