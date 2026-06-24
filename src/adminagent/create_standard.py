"""workflows/create_standard.py — server-only."""

from typing import Any
from agent_framework import Executor, WorkflowContext, handler, response_handler
from messages import CollectedParams, WorkflowResult
from mcp_client import call_mcp
from request_compat import rget
from action_queue import advance_or_finish


class ConfirmCreateExecutor(Executor):
    def __init__(self):
        super().__init__(id="confirm_create_std")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        display = p.get("display_name") or p["name"]
        await ctx.request_info(
            request_data={
                "_type": "confirm",
                "message": (
                    f"Create new standard?\n"
                    f"  Slug:         {p['name']}\n"
                    f"  Display name: {display}\n"
                    f"Type 'yes' to confirm or 'no' to cancel:"
                ),
                "choices": ["yes", "no"],
                "carry": p,
            },
            response_type=str,
        )

    @response_handler
    async def handle_confirm(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        carry = rget(original_request, "carry", {})
        if response.strip().lower() not in ("yes", "y", "confirm"):
            await ctx.yield_output(WorkflowResult(status="cancelled", message="Standard creation cancelled."))
            return
        await ctx.send_message(CollectedParams(params=carry))


class CreateStandardExecutor(Executor):
    def __init__(self):
        super().__init__(id="create_standard")

    @handler
    async def handle(self, request: CollectedParams, ctx: WorkflowContext) -> None:
        p = request.params
        args = {"name": p["name"], "display_name": p.get("display_name") or p["name"]}

        result = await call_mcp("create_standard", args)
        if "error" in result:
            await ctx.yield_output(WorkflowResult(status="failed", message=f"Creation failed: {result['error']}"))
            return

        warnings = result.get("warnings", [])
        await advance_or_finish(ctx, WorkflowResult(
            status="success",
            message=f"✓ Standard '{result.get('name')}' created (display: '{result.get('display_name')}').",
            warnings=warnings,
        ))