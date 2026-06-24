"""router_workflow.py — single top-level MAF workflow for server deployment.

v22:
  - Uses action_queue.build_route_message() as the single source of truth
    for action -> _route dict shape (was previously duplicated inline here
    and would have duplicated again in action_queue.py otherwise).
  - Router LLM may now emit a JSON array for multi-action messages
    ("add bilal to organic then add him to subgroup reg1"). First action
    runs immediately; the rest are queued via ctx.get_state/set_state
    (JSON-safe, same mechanism as chat_history) and picked up by
    advance_or_finish() at the end of each action's leaf executor.
  - Fixed a latent bug: if the LLM ever emitted a JSON array instead of an
    object, the old code's `parsed.get("action", ...)` would crash with
    AttributeError since a list has no .get(). Now handled explicitly.
  - Added explicit instruction not to split a slash-containing path value
    on the word before the slash (caused 'Organic/interpretive' -> 'interpretive').
  - GENERAL_SYSTEM_PROMPT now mentions search_users_tool.
"""

import json
from agent_framework import Executor, WorkflowContext, WorkflowBuilder, handler, response_handler, Message
from agent_framework_foundry import FoundryChatClient
from messages import WorkflowResult, ParamSpec, CollectedParams
from mcp_client import call_mcp
from agent_tools import AGENT_TOOLS
from action_queue import build_route_message
from typing import Any

from add_user_to_standard import SearchUsersExecutor as StdSearchExec, AssignStandardExecutor
from add_user_to_subgroup import SearchUsersExecutor as SgSearchExec, AssignSubgroupExecutor
from create_standard import ConfirmCreateExecutor, CreateStandardExecutor
from create_subgroup import ConfirmCreateSubgroupExecutor, CreateSubgroupExecutor
from grant_revoke_path import PathPickExecutor, ExecutePathExecutor
from remove_user import SearchUsersExecutor as RmSearchExec, ConfirmRemoveExecutor, RemoveExecutor


CANCEL_TOKENS = {"cancel", "stop", "quit", "nevermind", "never mind", "__cancel__"}
MAX_HISTORY_TURNS = 6


def _append_history(ctx: WorkflowContext, role: str, text: str) -> list[dict]:
    history = ctx.get_state("chat_history", default=[])
    history = history + [{"role": role, "text": text}]
    history = history[-MAX_HISTORY_TURNS:]
    ctx.set_state("chat_history", history)
    return history


def _history_as_text(history: list[dict]) -> str:
    if not history:
        return ""
    return "Recent conversation:\n" + "\n".join(f"{h['role']}: {h['text']}" for h in history) + "\n\n"


def _specs_to_list(specs: list[ParamSpec]) -> list[dict]:
    return [{"field": s.field, "prompt": s.prompt, "choices": s.choices} for s in specs]

def _list_to_specs(specs: list[dict]) -> list[ParamSpec]:
    return [ParamSpec(field=s["field"], prompt=s["prompt"], choices=s.get("choices", [])) for s in specs]


async def _collect_dict(specs: list[dict], current: dict, ctx: WorkflowContext) -> None:
    for i, spec in enumerate(specs):
        field   = spec["field"]
        choices = spec.get("choices", [])

        if field == "slug" and current.get("scope") == "standard":
            current["slug"] = ""
            continue

        val = current.get(field, "").strip()
        if choices and val.lower() not in [c.lower() for c in choices]:
            val = ""

        if not val:
            await ctx.request_info(
                request_data={
                    "_type": "param_ask", "field": field, "prompt": spec["prompt"],
                    "choices": choices, "current": current, "remaining_specs": specs[i+1:],
                },
                response_type=str,
            )
            return

    await ctx.send_message(CollectedParams(params=current))


async def _resume_collect(original_request: Any, response: str, ctx: WorkflowContext) -> None:
    if response.strip().lower() in CANCEL_TOKENS:
        await ctx.yield_output(WorkflowResult(
            status="cancelled", message="Okay, cancelled. What would you like to do instead?",
        ))
        return

    current  = dict(original_request["current"])
    specs    = original_request["remaining_specs"]
    field    = original_request["field"]
    choices  = original_request.get("choices", [])
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
                    message=f"Invalid number. Pick between 1 and {len(choices)}, or type 'cancel'.",
                ))
                return
        elif value.lower() not in choice_values:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"'{value}' is not valid. Choose from: {', '.join(choices)} (or type 'cancel').",
            ))
            return
        else:
            value = value.lower()

    if not value:
        await ctx.yield_output(WorkflowResult(
            status="failed", message=f"'{field}' cannot be empty. Try again, or type 'cancel'.",
        ))
        return

    current[field] = value
    await _collect_dict(list(specs), current, ctx)


ROUTER_SYSTEM_PROMPT = """You classify a WFCF admin message into one of seven buckets.
Emit ONLY a JSON object — nothing else, no explanation, no markdown fences.

If the user's intent is clearly one of these six actions — adding, removing, creating,
or granting/revoking something — emit that action's JSON even if most fields are blank.
Use "" for anything not mentioned. Blank fields are expected and fine; a later step
collects them interactively. Do NOT withhold the action JSON just because a field
is missing — intent is what matters, not completeness.

{"action": "add_user_to_standard", "standard": "", "tier": "", "user_query": ""}
{"action": "add_user_to_subgroup", "standard": "", "slug": "", "user_query": ""}
{"action": "create_standard", "name": "", "display_name": ""}
{"action": "create_subgroup", "standard": "", "slug": "", "display_name": ""}
{"action": "grant_revoke_path", "standard": "", "slug": "", "action_type": "", "path": ""}
{"action": "remove_user", "standard": "", "scope": "", "slug": "", "user_query": ""}

Examples — intent is clear even though most fields are blank, these are NOT "general":
"add user in subgroup" -> {"action": "add_user_to_subgroup", "standard": "", "slug": "", "user_query": ""}
"add user bilal" -> {"action": "add_user_to_standard", "standard": "", "tier": "", "user_query": "bilal"}
"add bilal to organic" -> {"action": "add_user_to_standard", "standard": "organic", "tier": "", "user_query": "bilal"}
"create a subgroup" -> {"action": "create_subgroup", "standard": "", "slug": "", "display_name": ""}
"revoke a path" -> {"action": "grant_revoke_path", "standard": "", "slug": "", "action_type": "revoke", "path": ""}
"remove someone" -> {"action": "remove_user", "standard": "", "scope": "", "slug": "", "user_query": ""}

PATH VALUES: when a field contains a slash (e.g. "Organic/interpretive"), copy it
COMPLETELY into "path" exactly as written. Never split off the part before the
slash, even if it looks like a standard name — the standard is a separate field
and is usually NOT given in the same message as the path.
"revoke Organic/interpretive from reg1" -> {"action": "grant_revoke_path", "standard": "", "slug": "reg1", "action_type": "revoke", "path": "Organic/interpretive"}

MULTIPLE ACTIONS: if the user clearly describes more than one action in one
message ("add bilal to organic then add him to subgroup reg1"), emit a JSON
ARRAY of action objects in the order they should run, instead of a single
object. Only do this when multiple distinct actions are genuinely described —
most messages still get a single object, not an array.

Use "general" ONLY when there is no actionable intent at all — greetings, small talk,
thanks, "how are you", "what can you do", or any read-only question (listing
standards/members/subgroups/paths, searching documents, looking up a user):
{"action": "general"}
"""

GENERAL_SYSTEM_PROMPT = """You are the WFCF Standards admin assistant.

For greetings, small talk, or unclear messages: reply naturally and briefly.
For read-only questions, call the right tool:
- "list standards" -> list_standards_tool
- "list members of standard X" -> list_standard_members_tool(standard="X")
- "list subgroups of X" -> list_subgroups_tool(standard="X")
- "list members of subgroup X in Y" -> list_subgroup_members_tool(standard="Y", slug="X")
- "list paths of subgroup X in Y" -> list_subgroup_paths_tool(standard="Y", slug="X")
- "search documents about X" -> search_documents_tool(query="X")
- "who is X" / "find user X" / "look up X" -> search_users_tool(query="X")
Never answer list/search questions from memory — always call the tool.
If the user actually wants to add/remove/create/grant something, tell them to say
so directly (e.g. "add bilal to organic") so the action flow can pick it up.
"""


class RouterExecutor(Executor):
    def __init__(self, client: FoundryChatClient, model: str):
        super().__init__(id="router")
        self._client = client
        self._model  = model

    @handler
    async def handle(self, request: list[Message], ctx: WorkflowContext) -> None:
        user_text = " ".join(
            part.get("content", "") if isinstance(part, dict) else str(part)
            for msg in request
            for part in (msg.contents if hasattr(msg, "contents") else [])
            if (part.get("type") if isinstance(part, dict) else getattr(part, "type", "")) == "text"
        ) or (request[-1].text if request else "")

        history = ctx.get_state("chat_history", default=[])
        prompt  = _history_as_text(history) + f"user: {user_text}"

        agent    = self._client.as_agent(name="router_llm", instructions=ROUTER_SYSTEM_PROMPT)
        response = await agent.run(prompt)
        text     = response.text.strip()

        try:
            clean   = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            decoded = json.loads(clean)
        except json.JSONDecodeError:
            decoded = {"action": "general"}

        # Router may emit a single object or a JSON array for multi-action
        # messages. Normalize to a list so a stray array can never crash
        # the old `.get("action", ...)` call (lists have no .get()).
        actions = decoded if isinstance(decoded, list) else [decoded]
        if not actions:
            actions = [{"action": "general"}]

        first, rest = actions[0], actions[1:]
        action = first.get("action", "general") if isinstance(first, dict) else "general"
        parsed = first if isinstance(first, dict) else {}

        if action != "general":
            _append_history(ctx, "user", user_text)

        if rest:
            # Plain list of dicts — JSON-safe, scoped per conversation via
            # the checkpoint chain, same as chat_history.
            ctx.set_state("action_queue", rest)

        route_msg = build_route_message(action, parsed)
        if route_msg:
            await ctx.send_message(route_msg)
        else:
            await ctx.send_message({"_route": "general", "query": user_text})


class BridgeAddUserStandard(Executor):
    def __init__(self): super().__init__(id="bridge_add_user_std")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "add_user_to_standard":
            return
        std_result = await call_mcp("list_standards", {})
        standards  = std_result.get("standards", []) if "error" not in std_result else []
        std_slugs  = [s["name"] for s in standards]
        std_prompt = "Which standard?\n" + "\n".join(
            f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}" for i, s in enumerate(standards)
        )
        specs = [
            {"field": "standard",   "prompt": std_prompt,                  "choices": std_slugs},
            {"field": "tier",       "prompt": "Add as 'user' or 'admin'?", "choices": ["user", "admin"]},
            {"field": "user_query", "prompt": "Name of the person to add?","choices": []},
        ]
        initial = {"standard": request.get("standard", ""), "tier": request.get("tier", ""),
                   "user_query": request.get("user_query", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class BridgeAddUserSubgroup(Executor):
    def __init__(self): super().__init__(id="bridge_add_user_sg")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "add_user_to_subgroup":
            return
        std_result = await call_mcp("list_standards", {})
        standards  = std_result.get("standards", []) if "error" not in std_result else []
        std_slugs  = [s["name"] for s in standards]
        std_prompt = "Which standard?\n" + "\n".join(
            f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}" for i, s in enumerate(standards)
        )
        sg_slugs, sg_prompt = [], "Which subgroup slug?"
        known_std = request.get("standard", "")
        if known_std and known_std in std_slugs:
            sg_result = await call_mcp("list_subgroups", {"standard": known_std})
            sgs       = sg_result.get("subgroups", []) if "error" not in sg_result else []
            sg_slugs  = [s["slug"] for s in sgs]
            sg_prompt = "Which subgroup?\n" + "\n".join(
                f"  {i+1}. {s['slug']} — {s.get('display_name', s['slug'])}" for i, s in enumerate(sgs)
            )
        specs = [
            {"field": "standard",   "prompt": std_prompt,                   "choices": std_slugs},
            {"field": "slug",       "prompt": sg_prompt,                    "choices": sg_slugs},
            {"field": "user_query", "prompt": "Name of the person to add?", "choices": []},
        ]
        initial = {"standard": request.get("standard", ""), "slug": request.get("slug", ""),
                   "user_query": request.get("user_query", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class BridgeCreateStandard(Executor):
    def __init__(self): super().__init__(id="bridge_create_std")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "create_standard":
            return
        specs   = [{"field": "name", "prompt": "Standard slug (e.g. 'organic'):", "choices": []}]
        initial = {"name": request.get("name", ""), "display_name": request.get("display_name", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class BridgeCreateSubgroup(Executor):
    def __init__(self): super().__init__(id="bridge_create_sg")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "create_subgroup":
            return
        std_result = await call_mcp("list_standards", {})
        standards  = std_result.get("standards", []) if "error" not in std_result else []
        std_slugs  = [s["name"] for s in standards]
        std_prompt = "Which standard to create the subgroup in?\n" + "\n".join(
            f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}" for i, s in enumerate(standards)
        )
        specs = [
            {"field": "standard",     "prompt": std_prompt,                              "choices": std_slugs},
            {"field": "slug",         "prompt": "Subgroup slug (e.g. 'beer', 'reg1'):", "choices": []},
            {"field": "display_name", "prompt": "Display name (or press enter to use slug):", "choices": []},
        ]
        initial = {"standard": request.get("standard", ""), "slug": request.get("slug", ""),
                   "display_name": request.get("display_name", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class BridgeGrantRevokePath(Executor):
    def __init__(self): super().__init__(id="bridge_grant_revoke")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "grant_revoke_path":
            return
        std_result = await call_mcp("list_standards", {})
        standards  = std_result.get("standards", []) if "error" not in std_result else []
        std_slugs  = [s["name"] for s in standards]
        std_prompt = "Which standard?\n" + "\n".join(
            f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}" for i, s in enumerate(standards)
        )
        sg_slugs, sg_prompt = [], "Which subgroup slug?"
        known_std = request.get("standard", "")
        if known_std and known_std in std_slugs:
            sg_result = await call_mcp("list_subgroups", {"standard": known_std})
            sgs       = sg_result.get("subgroups", []) if "error" not in sg_result else []
            sg_slugs  = [s["slug"] for s in sgs]
            sg_prompt = "Which subgroup?\n" + "\n".join(
                f"  {i+1}. {s['slug']} — {s.get('display_name', s['slug'])}" for i, s in enumerate(sgs)
            )
        specs = [
            {"field": "standard", "prompt": std_prompt,         "choices": std_slugs},
            {"field": "slug",     "prompt": sg_prompt,          "choices": sg_slugs},
            {"field": "action",   "prompt": "Grant or revoke?", "choices": ["grant", "revoke"]},
        ]
        initial = {"standard": request.get("standard", ""), "slug": request.get("slug", ""),
                   "action": request.get("action", ""), "path": request.get("path", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class BridgeRemoveUser(Executor):
    def __init__(self): super().__init__(id="bridge_remove_user")

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "remove_user":
            return
        std_result = await call_mcp("list_standards", {})
        standards  = std_result.get("standards", []) if "error" not in std_result else []
        std_slugs  = [s["name"] for s in standards]
        std_prompt = "Which standard?\n" + "\n".join(
            f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}" for i, s in enumerate(standards)
        )
        sg_slugs, sg_prompt = [], "Which subgroup slug?"
        known_std   = request.get("standard", "")
        known_scope = request.get("scope", "")
        if known_std and known_std in std_slugs and known_scope == "subgroup":
            sg_result = await call_mcp("list_subgroups", {"standard": known_std})
            sgs       = sg_result.get("subgroups", []) if "error" not in sg_result else []
            sg_slugs  = [s["slug"] for s in sgs]
            sg_prompt = "Which subgroup?\n" + "\n".join(
                f"  {i+1}. {s['slug']} — {s.get('display_name', s['slug'])}" for i, s in enumerate(sgs)
            )
        specs = [
            {"field": "standard",   "prompt": std_prompt,                               "choices": std_slugs},
            {"field": "scope",      "prompt": "Remove from 'standard' or 'subgroup'?",  "choices": ["standard", "subgroup"]},
            {"field": "slug",       "prompt": sg_prompt,                                "choices": sg_slugs},
            {"field": "user_query", "prompt": "Name of the person to remove?",          "choices": []},
        ]
        initial = {"standard": request.get("standard", ""), "scope": request.get("scope", ""),
                   "slug": request.get("slug", ""), "user_query": request.get("user_query", "")}
        await _collect_dict(specs, initial, ctx)

    @response_handler
    async def handle_response(self, original_request: Any, response: str, ctx: WorkflowContext) -> None:
        await _resume_collect(original_request, response, ctx)


class GeneralQueryExecutor(Executor):
    def __init__(self, client: FoundryChatClient, model: str):
        super().__init__(id="general_query")
        self._client = client
        self._model  = model

    @handler
    async def handle(self, request: dict, ctx: WorkflowContext) -> None:
        if request.get("_route") != "general":
            return
        query   = request.get("query", "")
        history = ctx.get_state("chat_history", default=[])
        prompt  = _history_as_text(history) + f"user: {query}"

        agent    = self._client.as_agent(name="general_llm", instructions=GENERAL_SYSTEM_PROMPT)
        response = await agent.run(prompt, tools=AGENT_TOOLS)
        reply    = response.text.strip()

        _append_history(ctx, "user", query)
        _append_history(ctx, "assistant", reply)

        await ctx.yield_output(WorkflowResult(status="success", message=reply))


def build_router_workflow(client: FoundryChatClient, model: str):
    router         = RouterExecutor(client=client, model=model)
    bridge_add_std = BridgeAddUserStandard()
    bridge_add_sg  = BridgeAddUserSubgroup()
    bridge_crt_std = BridgeCreateStandard()
    bridge_crt_sg  = BridgeCreateSubgroup()
    bridge_grv     = BridgeGrantRevokePath()
    bridge_rm      = BridgeRemoveUser()
    general_query  = GeneralQueryExecutor(client=client, model=model)

    std_search = StdSearchExec(); std_assign = AssignStandardExecutor()
    sg_search  = SgSearchExec();  sg_assign  = AssignSubgroupExecutor()
    crt_std_cf = ConfirmCreateExecutor(); crt_std_ex = CreateStandardExecutor()
    crt_sg_cf  = ConfirmCreateSubgroupExecutor(); crt_sg_ex = CreateSubgroupExecutor()
    path_pick  = PathPickExecutor(); path_exec = ExecutePathExecutor()
    rm_search  = RmSearchExec(); rm_confirm = ConfirmRemoveExecutor(); rm_exec = RemoveExecutor()

    workflow = (
        WorkflowBuilder(start_executor=router)
        .add_edge(router, bridge_add_std)
        .add_edge(router, bridge_add_sg)
        .add_edge(router, bridge_crt_std)
        .add_edge(router, bridge_crt_sg)
        .add_edge(router, bridge_grv)
        .add_edge(router, bridge_rm)
        .add_edge(router, general_query)
        .add_edge(bridge_add_std, std_search)
        .add_edge(std_search, std_assign)
        .add_edge(bridge_add_sg, sg_search)
        .add_edge(sg_search, sg_assign)
        .add_edge(bridge_crt_std, crt_std_cf)
        .add_edge(crt_std_cf, crt_std_ex)
        .add_edge(bridge_crt_sg, crt_sg_cf)
        .add_edge(crt_sg_cf, crt_sg_ex)
        .add_edge(bridge_grv, path_pick)
        .add_edge(path_pick, path_exec)
        .add_edge(bridge_rm, rm_search)
        .add_edge(rm_search, rm_confirm)
        .add_edge(rm_confirm, rm_exec)
        .build()
    )
    return workflow