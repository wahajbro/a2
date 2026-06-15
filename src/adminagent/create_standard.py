"""workflows/create_standard.py

Flow:
  ParamCollectorExecutor  → collects: name (slug), display_name (optional — defaults to name)
  ConfirmExecutor         → shows summary and asks yes/no
  CreateStandardExecutor  → calls create_standard MCP
"""

#from __future__ import annotations
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler
from messages import CollectedParams, ConfirmRequest, WorkflowResult, ParamSpec
from param_collector import ParamCollectorExecutor
from mcp_client import call_mcp


# ── Executor 1: Confirm ───────────────────────────────────────────────────────

class ConfirmCreateExecutor(Executor):
    def __init__(self):
        super().__init__(id="confirm_create_std")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        display = p.get("display_name") or p["name"]
        await ctx.request_info(
            request_data=ConfirmRequest(
                message=(
                    f"Create new standard?\n"
                    f"  Slug:         {p['name']}\n"
                    f"  Display name: {display}\n"
                    f"Type 'yes' to confirm or 'no' to cancel:"
                ),
                carry=p,
            ),
            response_type=str,
        )

    @response_handler
    async def handle_confirm(self, original_request: ConfirmRequest, response: str, ctx: WorkflowContext) -> None:
        if response.strip().lower() not in ("yes", "y"):
            await ctx.yield_output(WorkflowResult(status="cancelled", message="Standard creation cancelled."))
            return
        await ctx.send_message(CollectedParams(params=original_request.carry))


# ── Executor 2: Create ────────────────────────────────────────────────────────

class CreateStandardExecutor(Executor):
    def __init__(self):
        super().__init__(id="create_standard")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        args = {
            "name":         p["name"],
            "display_name": p.get("display_name") or p["name"],
        }

        result = await call_mcp("create_standard", args)
        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Creation failed: {result['error']}"))
            return

        warnings = result.get("warnings", [])
        await ctx.yield_output(WorkflowResult(
            status="success",
            message=(
                f"✓ Standard '{result.get('name')}' created "
                f"(display: '{result.get('display_name')}')."
            ),
            warnings=warnings,
        ))


# ── Builder ───────────────────────────────────────────────────────────────────

async def build(extracted: dict) -> tuple:
    specs = [
        ParamSpec("name", "Standard slug (e.g. 'organic', 'iso9001'):", choices=[]),
    ]

    collector = ParamCollectorExecutor(specs=specs, initial=extracted)
    confirm   = ConfirmCreateExecutor()
    create    = CreateStandardExecutor()

    workflow = (
        WorkflowBuilder(start_executor=collector)
        .add_edge(collector, confirm)
        .add_edge(confirm, create)
        .build()
    )
    return workflow, extracted
