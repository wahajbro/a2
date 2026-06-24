"""action_queue.py — shared LLM-detected multi-action chaining.

Stores a JSON-safe queue of pending actions in checkpointed state
(same ctx.get_state/set_state mechanism as chat_history — automatically
scoped per conversation, no manual session key needed).
"""

from agent_framework import WorkflowContext


def build_route_message(action: str, parsed: dict) -> dict | None:
    """Single source of truth for action -> _route dict shape, so the
    router's first action and the queue's later actions never drift."""
    if action == "add_user_to_standard":
        return {"_route": "add_user_to_standard", "standard": parsed.get("standard", ""),
                "tier": parsed.get("tier", ""), "user_query": parsed.get("user_query", "")}
    if action == "add_user_to_subgroup":
        return {"_route": "add_user_to_subgroup", "standard": parsed.get("standard", ""),
                "slug": parsed.get("slug", ""), "user_query": parsed.get("user_query", "")}
    if action == "create_standard":
        return {"_route": "create_standard", "name": parsed.get("name", ""),
                "display_name": parsed.get("display_name", "")}
    if action == "create_subgroup":
        return {"_route": "create_subgroup", "standard": parsed.get("standard", ""),
                "slug": parsed.get("slug", ""), "display_name": parsed.get("display_name", "")}
    if action == "grant_revoke_path":
        return {"_route": "grant_revoke_path", "standard": parsed.get("standard", ""),
                "slug": parsed.get("slug", ""), "action": parsed.get("action_type", ""),
                "path": parsed.get("path", "")}
    if action == "remove_user":
        return {"_route": "remove_user", "standard": parsed.get("standard", ""),
                "scope": parsed.get("scope", ""), "slug": parsed.get("slug", ""),
                "user_query": parsed.get("user_query", "")}
    return None


async def advance_or_finish(ctx: WorkflowContext, result) -> None:
    """Call this instead of a bare ctx.yield_output(result) at the end of
    every action workflow's final leaf executor. If more actions are
    queued, yields this result (so the user sees it) AND kicks off the
    next action in the same run; otherwise behaves exactly as before."""
    await ctx.yield_output(result)

    queue = ctx.get_state("action_queue", default=[])
    if not queue:
        return

    next_action = queue[0]
    remaining   = queue[1:]
    ctx.set_state("action_queue", remaining)

    route_msg = build_route_message(next_action.get("action", ""), next_action)
    if route_msg:
        await ctx.send_message(route_msg)