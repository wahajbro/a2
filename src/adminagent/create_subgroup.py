"""workflows/create_subgroup.py

Flow:
  ParamCollectorExecutor   → collects: standard, slug, display_name
  ConfirmExecutor          → shows summary, asks yes/no
  CreateSubgroupExecutor   → calls create_subgroup MCP
"""

#from __future__ import annotations
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler
from messages import CollectedParams, ConfirmRequest, WorkflowResult, ParamSpec
from param_collector import ParamCollectorExecutor
from mcp_client import call_mcp


# ── Executor 1: Confirm ───────────────────────────────────────────────────────

class ConfirmCreateSubgroupExecutor(Executor):
    def __init__(self):
        super().__init__(id="confirm_create_sg")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        await ctx.request_info(
            request_data=ConfirmRequest(
                message=(
                    f"Create new subgroup?\n"
                    f"  Standard:     {p['standard']}\n"
                    f"  Slug:         {p['slug']}\n"
                    f"  Display name: {p['display_name']}\n"
                    f"Type 'yes' to confirm or 'no' to cancel:"
                ),
                carry=p,
            ),
            response_type=str,
        )

    @response_handler
    async def handle_confirm(self, original_request: ConfirmRequest, response: str, ctx: WorkflowContext) -> None:
        if response.strip().lower() not in ("yes", "y"):
            await ctx.yield_output(WorkflowResult(status="cancelled", message="Subgroup creation cancelled."))
            return
        await ctx.send_message(CollectedParams(params=original_request.carry))


# ── Executor 2: Create ────────────────────────────────────────────────────────

class CreateSubgroupExecutor(Executor):
    def __init__(self):
        super().__init__(id="create_subgroup")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        result = await call_mcp("create_subgroup", {
            "standard":     p["standard"],
            "slug":         p["slug"],
            "display_name": p.get("display_name") or p["slug"],
        })

        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Creation failed: {result['error']}"))
            return

        warnings = result.get("warnings", [])
        await ctx.yield_output(WorkflowResult(
            status="success",
            message=(
                f"✓ Subgroup '{result.get('slug')}' created under '{result.get('standard')}' "
                f"(display: '{result.get('display_name')}')."
            ),
            warnings=warnings,
        ))


# ── Builder ───────────────────────────────────────────────────────────────────

async def build(extracted: dict) -> tuple:
    std_result = await call_mcp("list_standards", {})
    standards  = std_result.get("standards", []) if "error" not in std_result else []
    std_slugs  = [s["name"] for s in standards]
    std_prompt = "Which standard to create the subgroup in?\n" + "\n".join(
        f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}"
        for i, s in enumerate(standards)
    )

    # new
    specs = [
        ParamSpec("standard", std_prompt,                          choices=std_slugs),
        ParamSpec("slug",     "Subgroup name (e.g. 'beer', 'reg1'):", choices=[]),
    ]

    collector = ParamCollectorExecutor(specs=specs, initial=extracted)
    confirm   = ConfirmCreateSubgroupExecutor()
    create    = CreateSubgroupExecutor()

    workflow = (
        WorkflowBuilder(start_executor=collector)
        .add_edge(collector, confirm)
        .add_edge(confirm, create)
        .build()
    )
    return workflow, extracted
