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
"""

#from __future__ import annotations
from agent_framework import Executor, WorkflowContext, handler, response_handler
from messages import ParamSpec, ParamAskRequest, CollectedParams, WorkflowResult


class ParamCollectorExecutor(Executor):
    def __init__(self, specs: list[ParamSpec], initial: dict):
        super().__init__(id="param_collector")
        self._specs   = specs
        self._initial = initial

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        # request here is the initial dict passed in — we use self._initial
        await _collect(self._specs, dict(self._initial), ctx)

    @response_handler
    async def handle_response(
        self,
        original_request: ParamAskRequest,
        response: str,
        ctx: WorkflowContext,
    ) -> None:
        current  = dict(original_request.current)
        specs    = original_request.remaining_specs  # specs still to check after this field
        field    = original_request.field
        choices  = original_request.choices
        value    = response.strip()

        # Validate choice-based fields
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

        # Continue checking remaining specs
        all_specs_from_here = [ParamSpec(field=original_request.field, prompt="", choices=choices)] + list(specs)
        # But field is now filled — just continue with remaining
        await _collect(list(specs), current, ctx)


# new
async def _collect(specs: list[ParamSpec], current: dict, ctx: WorkflowContext) -> None:
    """Walk specs in order; pause on first missing/invalid one."""
    for i, spec in enumerate(specs):
        # skip slug entirely when removing from standard
        if spec.field == "slug" and current.get("scope") == "standard":
            current["slug"] = ""
            continue

        val = current.get(spec.field, "").strip()

        # Validate against choices if provided
        if spec.choices and val.lower() not in [c.lower() for c in spec.choices]:
            val = ""  # treat as missing if invalid

        if not val:
            # Build prompt with numbered choices if available
            prompt = spec.prompt
            

            await ctx.request_info(
                request_data=ParamAskRequest(
                    field=spec.field,
                    prompt=prompt,
                    choices=spec.choices,
                    current=current,
                    remaining_specs=specs[i+1:],  # specs after this one
                ),
                response_type=str,
            )
            return  # pause — response_handler takes over

    # All specs satisfied — send forward
    await ctx.send_message(CollectedParams(params=current))
