"""param_collector.py — reusable code-enforced param collection executor.

Usage in any workflow:
    specs = [
        ParamSpec("standard", "Which standard?", choices=<fetched from MCP>),
        ParamSpec("tier",     "Add as 'user' or 'admin'?", choices=["user", "admin"]),
        ParamSpec("user_query", "Name of the person to add?"),
    ]
    collector = ParamCollectorExecutor(specs=specs, initial={"standard": "organic", "tier": ""})

The executor checks each spec in order:
  - If already in `initial` and valid → skip
  - If missing or invalid → request_info pause → human fills it → loops back
Once all collected → sends CollectedParams to next executor.

CHANGE vs original:
  Added handle_messages() — accepts list[ChatMessage] so that workflow.as_agent()
  works when WorkflowAgent feeds the start executor list[ChatMessage].
  The initial extracted dict is stored in self._initial at build time, so
  the message content is irrelevant — we just ignore it and use self._initial.
  This is the ONLY change. All pause/resume logic is untouched.
"""

#from __future__ import annotations
from agent_framework import Executor, WorkflowContext, handler, response_handler, Message
#from agent_framework import ChatMessage          # needed for list[ChatMessage] type hint
from messages import ParamSpec, ParamAskRequest, CollectedParams, WorkflowResult


class ParamCollectorExecutor(Executor):
    def __init__(self, specs: list[ParamSpec], initial: dict):
        super().__init__(id="param_collector")
        self._specs   = specs
        self._initial = initial

    # ── called by workflow_runner (CLI) with a plain dict ─────────────────────
    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        await _collect(self._specs, dict(self._initial), ctx)

    # ── called by WorkflowAgent (server) with list[ChatMessage] ──────────────
    # WorkflowAgent._normalize_messages() always passes list[ChatMessage].
    # The extracted params are already in self._initial from build(), so we
    # ignore the message content entirely and start collecting from self._initial.
    @handler
    async def handle_messages(self, request: list[Message], ctx: WorkflowContext) -> None:
        await _collect(self._specs, dict(self._initial), ctx)

    # ── response handler — same for both CLI and server paths ─────────────────
    @response_handler
    async def handle_response(
        self,
        original_request: ParamAskRequest,  
        response: str,
        ctx: WorkflowContext,
    ) -> None:
        current  = dict(original_request.current)
        specs    = original_request.remaining_specs
        field    = original_request.field
        choices  = original_request.choices
        value    = response.strip()

        if choices:
            choice_values = [c.lower() for c in choices]
            if value.isdigit():
                idx = int(value) - 1
                if 0 <= idx < len(choices):
                    value = choices[idx]
                else:
                    await ctx.yield_output(WorkflowResult(
                        status="failed",
                        message=f"Invalid number. Pick between 1 and {len(choices)}.",
                    ))
                    return
            elif value.lower() not in choice_values:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message=f"'{value}' is not valid. Choose from: {', '.join(choices)}",
                ))
                return
            else:
                value = value.lower()

        if not value:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"'{field}' cannot be empty. Please try again.",
            ))
            return

        current[field] = value
        await _collect(list(specs), current, ctx)


async def _collect(specs: list[ParamSpec], current: dict, ctx: WorkflowContext) -> None:
    """Walk specs in order; pause on first missing/invalid one."""
    for i, spec in enumerate(specs):
        # skip slug entirely when removing from standard scope
        if spec.field == "slug" and current.get("scope") == "standard":
            current["slug"] = ""
            continue

        val = current.get(spec.field, "").strip()

        if spec.choices and val.lower() not in [c.lower() for c in spec.choices]:
            val = ""

        if not val:
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field=spec.field,
                    prompt=spec.prompt,
                    choices=spec.choices,
                    current=current,
                    remaining_specs=specs[i+1:],
                ),
                response_type=str,
            )
            return

    await ctx.send_message(CollectedParams(params=current))