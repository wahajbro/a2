# """
# admin_agent.py — WFCF Admin Agent (MAF Executor + WorkflowBuilder approach)

# install:
#     pip install agent-framework-core agent-framework-foundry azure-identity python-dotenv httpx

# Why MAF Executors instead of tool chains:
#   - WorkflowBuilder + Executor guarantees each step runs in order in Python
#   - ctx.request_info() pauses workflow and waits for real human input — enforced by framework
#   - No LLM involved between steps — framework engine drives execution
#   - No cache dict needed — workflow state is managed by the framework
#   - No risk of LLM skipping turn 2 — framework calls next executor automatically

# Flow for add_user_to_standard:
#   SearchUsersExecutor   → calls search_users MCP, pauses for human pick via request_info
#   AssignUserExecutor    → receives picked OID, calls assign_user_to_standard MCP

# The main agent (LLM) handles ONLY:
#   - Intent detection
#   - Param extraction from natural language (standard, tier, user_query)
#   - Validating standard exists via list_standards tool before firing workflow
#   - Asking only for missing params — never re-asking what user already said
# """

# import asyncio
# import json
# import os
# from dataclasses import dataclass, field
# from dotenv import load_dotenv
# import httpx
# from azure.identity import AzureCliCredential

# # ── MAF imports ───────────────────────────────────────────────────────────────
# from agent_framework import (
#     Executor,
#     WorkflowBuilder,
#     WorkflowContext,
#     WorkflowEvent,
#     handler,
#     response_handler,
# )
# from agent_framework_foundry import FoundryChatClient

# load_dotenv()

# PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
# MODEL_DEPLOYMENT  = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
# MCP_BASE_URL      = os.getenv("MCP_BASE_URL", "http://localhost:8000")
# MCP_TOKEN         = os.getenv("MCP_TOKEN", "")


# # ── MCP caller ────────────────────────────────────────────────────────────────

# async def call_mcp(tool_name: str, args: dict) -> dict:
#     headers = {"Content-Type": "application/json"}
#     if MCP_TOKEN:
#         headers["Authorization"] = f"Bearer {MCP_TOKEN}"
#     payload = {
#         "jsonrpc": "2.0", "method": "tools/call",
#         "params": {"name": tool_name, "arguments": args}, "id": 1,
#     }
#     async with httpx.AsyncClient(timeout=30) as client:
#         try:
#             r = await client.post(f"{MCP_BASE_URL}/mcp", json=payload, headers=headers)
#             r.raise_for_status()
#             body = r.json()
#             if "error" in body:
#                 return {"error": body["error"].get("message", "unknown")}
#             result = body.get("result", {})
#             if "content" in result and result["content"]:
#                 try:
#                     return json.loads(result["content"][0]["text"])
#                 except Exception:
#                     return {"text": result["content"][0]["text"]}
#             return result
#         except Exception as e:
#             return {"error": str(e)}


# # ── Agent tools (callable by LLM during conversation) ─────────────────────────

# async def list_standards_tool() -> str:
#     """List all available WFCF standards the caller can administer."""
#     result = await call_mcp("list_standards", {})
#     if "error" in result:
#         return f"Error fetching standards: {result['error']}"
#     standards = result.get("standards", [])
#     if not standards:
#         return "No standards found."
#     return json.dumps([
#         {"name": s["name"], "display_name": s.get("display_name", s["name"])}
#         for s in standards
#     ])


# async def list_standard_members_tool(standard: str) -> str:
#     """List current members of a standard."""
#     result = await call_mcp("list_standard_members", {"standard": standard})
#     if "error" in result:
#         return f"Error: {result['error']}"
#     members = result.get("members", [])
#     return json.dumps([
#         {
#             "name":  m.get("displayName") or m.get("display_name", ""),
#             "email": m.get("mail") or m.get("email") or m.get("userPrincipalName", ""),
#             "tier":  m.get("tier", ""),
#         }
#         for m in members
#     ])


# # ─────────────────────────────────────────────────────────────────────────────
# # MESSAGE TYPES
# # ─────────────────────────────────────────────────────────────────────────────

# @dataclass
# class AddUserRequest:
#     standard: str
#     tier: str        # "user" or "admin"
#     user_query: str  # partial name or email from human

# @dataclass
# class UserPickRequest:
#     matches: list    # list of {name, email, oid}
#     standard: str
#     tier: str

# @dataclass
# class AssignRequest:
#     standard: str
#     tier: str
#     user_oid: str
#     user_name: str
#     user_email: str

# @dataclass
# class WorkflowResult:
#     status: str      # "success" | "failed" | "already_done"
#     message: str
#     warnings: list = field(default_factory=list)


# # ─────────────────────────────────────────────────────────────────────────────
# # EXECUTOR 1: SearchUsersExecutor
# # ─────────────────────────────────────────────────────────────────────────────

# class SearchUsersExecutor(Executor):
#     def __init__(self):
#         super().__init__(id="search_users")

#     @handler
#     async def handle(self, request: AddUserRequest, ctx: WorkflowContext) -> None:
#         result = await call_mcp("search_users", {"query": request.user_query})
#         if "error" in result:
#             await ctx.yield_output(WorkflowResult(
#                 status="failed",
#                 message=f"Search failed: {result['error']}",
#             ))
#             return

#         users = result.get("users", [])
#         if not users:
#             await ctx.yield_output(WorkflowResult(
#                 status="failed",
#                 message=f"No user found matching '{request.user_query}'. Try a different name or email.",
#             ))
#             return

#         matches = [
#             {
#                 "name":  u.get("displayName") or u.get("display_name") or "Unknown",
#                 "email": u.get("mail") or u.get("userPrincipalName") or u.get("email") or "",
#                 "oid":   u.get("id") or u.get("oid") or "",
#             }
#             for u in users[:20]
#         ]

#         # Pause here — human must pick before AssignUserExecutor runs
#         await ctx.request_info(
#             request_data=UserPickRequest(
#                 matches=matches,
#                 standard=request.standard,
#                 tier=request.tier,
#             ),
#             response_type=str,
#         )

#     @response_handler
#     async def handle_pick(
#         self,
#         original_request: UserPickRequest,
#         response: str,
#         ctx: WorkflowContext,
#     ) -> None:
#         matches = original_request.matches
#         chosen_oid = chosen_name = chosen_email = None

#         # Accept 1-based number
#         if response.strip().isdigit():
#             idx = int(response.strip()) - 1
#             if 0 <= idx < len(matches):
#                 chosen_oid   = matches[idx]["oid"]
#                 chosen_name  = matches[idx]["name"]
#                 chosen_email = matches[idx]["email"]
#             else:
#                 await ctx.yield_output(WorkflowResult(
#                     status="failed",
#                     message=f"Invalid number '{response}'. Pick between 1 and {len(matches)}.",
#                 ))
#                 return
#         else:
#             # Accept OID or email directly
#             for m in matches:
#                 if response.strip() in (m["oid"], m["email"]):
#                     chosen_oid   = m["oid"]
#                     chosen_name  = m["name"]
#                     chosen_email = m["email"]
#                     break

#         if not chosen_oid:
#             await ctx.yield_output(WorkflowResult(
#                 status="failed",
#                 message=f"'{response}' not found in results. Enter the number shown next to the user.",
#             ))
#             return

#         await ctx.send_message(AssignRequest(
#             standard=original_request.standard,
#             tier=original_request.tier,
#             user_oid=chosen_oid,
#             user_name=chosen_name,
#             user_email=chosen_email,
#         ))


# # ─────────────────────────────────────────────────────────────────────────────
# # EXECUTOR 2: AssignUserExecutor
# # ─────────────────────────────────────────────────────────────────────────────

# class AssignUserExecutor(Executor):
#     def __init__(self):
#         super().__init__(id="assign_user")

#     @handler
#     async def handle(self, request: AssignRequest, ctx: WorkflowContext) -> None:
#         result = await call_mcp("assign_user_to_standard", {
#             "standard": request.standard,
#             "user_oid": request.user_oid,
#             "tier":     request.tier,
#         })

#         if "error" in result:
#             await ctx.yield_output(WorkflowResult(
#                 status="failed",
#                 message=f"Assignment failed: {result['error']}",
#             ))
#             return

#         # assign_user_to_standard returns already_member=True if duplicate
#         if result.get("already_member"):
#             await ctx.yield_output(WorkflowResult(
#                 status="already_done",
#                 message=(
#                     f"{request.user_name} ({request.user_email}) is already "
#                     f"a {request.tier} of '{request.standard}' — no change made."
#                 ),
#             ))
#             return

#         await ctx.yield_output(WorkflowResult(
#             status="success",
#             message=(
#                 f"✓ Added {request.user_name} ({request.user_email}) "
#                 f"to '{request.standard}' as {request.tier}."
#             ),
#         ))


# # ─────────────────────────────────────────────────────────────────────────────
# # WORKFLOW BUILDER
# # ─────────────────────────────────────────────────────────────────────────────

# def build_add_user_workflow():
#     search_exec = SearchUsersExecutor()
#     assign_exec = AssignUserExecutor()
#     return (
#         WorkflowBuilder(start_executor=search_exec)
#         .add_edge(search_exec, assign_exec)
#         .build()
#     )


# # ─────────────────────────────────────────────────────────────────────────────
# # WORKFLOW RUNNER
# # ─────────────────────────────────────────────────────────────────────────────
# async def run_add_user_workflow(standard: str, tier: str, user_query: str) -> WorkflowResult:
#     workflow = build_add_user_workflow()
#     request  = AddUserRequest(standard=standard, tier=tier, user_query=user_query)

#     # Phase 1: run until request_info pause
#     resume_id = None
#     chosen = None

#     async for event in workflow.run(request, stream=True):
#         if event.type == "request_info":
#             pick_request: UserPickRequest = event.data
#             matches = pick_request.matches

#             print("\nMatching users:")
#             for i, m in enumerate(matches, 1):
#                 print(f"  {i}. {m['name']}  <{m['email']}>")
#             print()

#             chosen = input("Pick a number: ").strip()
#             resume_id = event.request_id
#             break

#         elif event.type == "output":
#             return event.data

#     if resume_id is None:
#         return WorkflowResult(status="failed", message="Workflow ended without requesting input.")

#     # Clone workflow to get a fresh runner + reset state for phase 2
#     workflow2 = workflow.clone()
#     workflow2._reset_running_flag()
#     workflow2._runner._running = False

#     # Phase 2: resume with human's response on the clean clone
#     async for event in workflow2.run(responses={resume_id: chosen}, stream=True):
#         if event.type == "output":
#             return event.data

#     return WorkflowResult(status="failed", message="Workflow ended without output after resume.")# ─────────────────────────────────────────────────────────────────────────────

# SYSTEM_PROMPT = """You are the WFCF Standards admin assistant.

# ## For "add user" requests, collect exactly 3 params:
#   - standard: the standard slug (e.g. "organic", "iso9001")
#   - tier: "user" or "admin" — ask ONLY if not mentioned
#   - user_query: name or partial email to search — extract from what user said

# ## Rules (follow strictly):
# 1. ALWAYS call list_standards_tool first to get real standards from the system.
# 2. If the user mentioned a standard name:
#    - Match it against the real list (case-insensitive, also match display_name).
#    - If it matches: use the slug directly, do NOT ask the user to confirm.
#    - If it does NOT match: show the real list and ask which one they meant.
# 3. Extract user_query and tier from the user's message.
#    - If tier is not mentioned: ask for it once.
#    - If user_query is not mentioned: ask for it once.
#    - NEVER ask for something the user already said.
# 4. Once all 3 params are confirmed, respond with ONLY this JSON — nothing else, no explanation:
#    {"action": "add_user", "standard": "<slug>", "tier": "<tier>", "user_query": "<query>"}

# ## For other requests:
# - "list standards" → call list_standards_tool, show results as a readable list.
# - "list members of X" or "who is in X" → call list_standard_members_tool(standard="X"), show name + email + tier.
# - Anything else → answer helpfully.

# ## Notes:
# - Duplicate member check is handled automatically — you do NOT need to check.
# - The user picks the actual person from real Azure AD search results shown to them.
# - Never invent or guess any param value.
# """


# # ─────────────────────────────────────────────────────────────────────────────
# # MAIN LOOP
# # ─────────────────────────────────────────────────────────────────────────────

# async def main():
#     client = FoundryChatClient(
#         project_endpoint=PROJECT_ENDPOINT,
#         model=MODEL_DEPLOYMENT,
#         credential=AzureCliCredential(),
#     )

#     agent = client.as_agent(
#         name="WFCFAdmin",
#         instructions=SYSTEM_PROMPT,
#     )

#     # Tools the LLM can call to verify standards and list members
#     agent_tools = [list_standards_tool, list_standard_members_tool]

#     print("WFCF Admin Assistant ready. Type 'quit' to exit.\n")

#     # Session keeps full conversation history — LLM remembers context across turns
#     session = agent.create_session()

#     while True:
#         user_input = input("You: ").strip()
#         if user_input.lower() in ("quit", "exit", "q"):
#             break
#         if not user_input:
#             continue

#         # LLM decides intent, validates standard, collects missing params
#         response = await agent.run(
#             user_input,
#             session=session,
#             tools=agent_tools,
#         )
#         text = response.text.strip()

#         # Detect ready-to-execute JSON action from LLM
#         try:
#             clean = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
#             parsed = json.loads(clean)

#             if parsed.get("action") == "add_user":
#                 standard   = parsed["standard"]
#                 tier       = parsed["tier"]
#                 user_query = parsed["user_query"]

#                 print(f"\nSearching for '{user_query}' to add to '{standard}' as {tier}...\n")
#                 result = await run_add_user_workflow(standard, tier, user_query)

#                 if result.status == "success":
#                     print(f"\n✓ {result.message}\n")
#                 elif result.status == "already_done":
#                     print(f"\nℹ  {result.message}\n")
#                 else:
#                     print(f"\n✗ {result.message}\n")
#             else:
#                 print(f"Agent: {text}\n")

#         except (json.JSONDecodeError, KeyError):
#             # Normal conversational LLM response
#             print(f"Agent: {text}\n")


# if __name__ == "__main__":
#     asyncio.run(main())
"""
admin_agent.py — WFCF Admin Agent (MAF Executor + WorkflowBuilder approach)

Flow for add_user_to_standard:
  ParamCollectorExecutor → checks standard/tier/user_query one by one via request_info
  SearchUsersExecutor    → calls search_users MCP, pauses for human pick via request_info
  AssignUserExecutor     → receives picked OID, calls assign_user_to_standard MCP

The main agent (LLM) handles ONLY:
  - Intent detection
  - Extracting whatever params the user already gave in their first message
  - Emitting JSON immediately if all 3 are present, or with blanks for missing ones
  - ParamCollectorExecutor fills in any gaps via code-enforced request_info pauses
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv
import httpx
from azure.identity import AzureCliCredential

from agent_framework import (
    Executor,
    WorkflowBuilder,
    WorkflowContext,
    WorkflowEvent,
    handler,
    response_handler,
)
from agent_framework_foundry import FoundryChatClient

load_dotenv()

PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT  = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")
MCP_BASE_URL      = os.getenv("MCP_BASE_URL", "http://localhost:8000")
MCP_TOKEN         = os.getenv("MCP_TOKEN", "")


# ── MCP caller ────────────────────────────────────────────────────────────────

async def call_mcp(tool_name: str, args: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if MCP_TOKEN:
        headers["Authorization"] = f"Bearer {MCP_TOKEN}"
    payload = {
        "jsonrpc": "2.0", "method": "tools/call",
        "params": {"name": tool_name, "arguments": args}, "id": 1,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{MCP_BASE_URL}/mcp", json=payload, headers=headers)
            r.raise_for_status()
            body = r.json()
            if "error" in body:
                return {"error": body["error"].get("message", "unknown")}
            result = body.get("result", {})
            if "content" in result and result["content"]:
                try:
                    return json.loads(result["content"][0]["text"])
                except Exception:
                    return {"text": result["content"][0]["text"]}
            return result
        except Exception as e:
            return {"error": str(e)}


# ── Agent tools (callable by LLM during conversation) ─────────────────────────

async def list_standards_tool() -> str:
    """List all available WFCF standards the caller can administer."""
    result = await call_mcp("list_standards", {})
    if "error" in result:
        return f"Error fetching standards: {result['error']}"
    standards = result.get("standards", [])
    if not standards:
        return "No standards found."
    return json.dumps([
        {"name": s["name"], "display_name": s.get("display_name", s["name"])}
        for s in standards
    ])


async def list_standard_members_tool(standard: str) -> str:
    """List current members of a standard."""
    result = await call_mcp("list_standard_members", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    return json.dumps([
        {
            "name":  m.get("displayName") or m.get("display_name", ""),
            "email": m.get("mail") or m.get("email") or m.get("userPrincipalName", ""),
            "tier":  m.get("tier", ""),
        }
        for m in members
    ])


# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE TYPES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AddUserRequest:
    # Any of these can be empty string — ParamCollectorExecutor will fill gaps
    standard: str
    tier: str
    user_query: str

@dataclass
class ParamAskRequest:
    # Used by ParamCollectorExecutor to ask human for one missing param at a time
    field: str        # "standard" | "tier" | "user_query"
    prompt: str
    current: dict     # carries whatever is already known so far
    standards: list   # full list from registry, used when asking for standard

@dataclass
class UserPickRequest:
    matches: list
    standard: str
    tier: str

@dataclass
class AssignRequest:
    standard: str
    tier: str
    user_oid: str
    user_name: str
    user_email: str

@dataclass
class WorkflowResult:
    status: str       # "success" | "failed" | "already_done"
    message: str
    warnings: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTOR 0: ParamCollectorExecutor
# ─────────────────────────────────────────────────────────────────────────────

class ParamCollectorExecutor(Executor):
    def __init__(self):
        super().__init__(id="param_collector")

    @handler
    async def handle(self, request: AddUserRequest, ctx: WorkflowContext) -> None:
        standards_result = await call_mcp("list_standards", {})
        standards = standards_result.get("standards", []) if "error" not in standards_result else []
        valid_slugs = [s["name"].lower() for s in standards]

        current = {
            "standard":   request.standard.strip(),
            "tier":       request.tier.strip(),
            "user_query": request.user_query.strip(),
        }

        # ── Check standard ────────────────────────────────────────────────────
        if not current["standard"] or current["standard"].lower() not in valid_slugs:
            standards_display = "\n".join(
                f"  {i+1}. {s['name']} — {s.get('display_name', s['name'])}"
                for i, s in enumerate(standards)
            )
            prompt = (
                f"Which standard?\n{standards_display}\n"
                "Enter the slug (e.g. organic) or number:"
            )
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field="standard",
                    prompt=prompt,
                    current=current,
                    standards=standards,
                ),
                response_type=str,
            )
            return

        # ── Check tier ────────────────────────────────────────────────────────
        if current["tier"] not in ("user", "admin"):
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field="tier",
                    prompt="Should this person be added as 'user' or 'admin'?",
                    current=current,
                    standards=standards,
                ),
                response_type=str,
            )
            return

        # ── Check user_query ──────────────────────────────────────────────────
        if not current["user_query"]:
            await ctx.request_info(
                request_data=ParamAskRequest(
                    field="user_query",
                    prompt="What is the name of the person you want to add?",
                    current=current,
                    standards=standards,
                ),
                response_type=str,
            )
            return

        # All 3 confirmed — forward to SearchUsersExecutor
        await ctx.send_message(AddUserRequest(
            standard=current["standard"],
            tier=current["tier"],
            user_query=current["user_query"],
        ))

    @response_handler
    async def handle_param_response(
        self,
        original_request: ParamAskRequest,
        response: str,
        ctx: WorkflowContext,
    ) -> None:
        current   = dict(original_request.current)
        standards = original_request.standards
        field     = original_request.field
        value     = response.strip()

        if field == "standard":
            valid_slugs = [s["name"].lower() for s in standards]
            if value.isdigit():
                idx = int(value) - 1
                if 0 <= idx < len(standards):
                    current["standard"] = standards[idx]["name"]
                else:
                    await ctx.yield_output(WorkflowResult(
                        status="failed",
                        message=f"Invalid number. Pick between 1 and {len(standards)}.",
                    ))
                    return
            elif value.lower() in valid_slugs:
                current["standard"] = value.lower()
            else:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message=f"'{value}' is not a valid standard. Please try again.",
                ))
                return

        elif field == "tier":
            if value.lower() not in ("user", "admin"):
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message="Tier must be 'user' or 'admin'. Please try again.",
                ))
                return
            current["tier"] = value.lower()

        elif field == "user_query":
            if not value:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message="Name cannot be empty. Please try again.",
                ))
                return
            current["user_query"] = value

        # Re-run collector with updated values
        await ctx.send_message(AddUserRequest(
            standard=current["standard"],
            tier=current["tier"],
            user_query=current["user_query"],
        ))


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTOR 1: SearchUsersExecutor
# ─────────────────────────────────────────────────────────────────────────────

class SearchUsersExecutor(Executor):
    def __init__(self):
        super().__init__(id="search_users")

    @handler
    async def handle(self, request: AddUserRequest, ctx: WorkflowContext) -> None:
        result = await call_mcp("search_users", {"query": request.user_query})
        if "error" in result:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"Search failed: {result['error']}",
            ))
            return

        users = result.get("users", [])
        if not users:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"No user found matching '{request.user_query}'. Try a different name.",
            ))
            return

        matches = [
            {
                "name":  u.get("displayName") or u.get("display_name") or "Unknown",
                "email": u.get("mail") or u.get("userPrincipalName") or u.get("email") or "",
                "oid":   u.get("id") or u.get("oid") or "",
            }
            for u in users[:20]
        ]

        await ctx.request_info(
            request_data=UserPickRequest(
                matches=matches,
                standard=request.standard,
                tier=request.tier,
            ),
            response_type=str,
        )

    @response_handler
    async def handle_pick(
        self,
        original_request: UserPickRequest,
        response: str,
        ctx: WorkflowContext,
    ) -> None:
        matches = original_request.matches
        chosen_oid = chosen_name = chosen_email = None

        if response.strip().isdigit():
            idx = int(response.strip()) - 1
            if 0 <= idx < len(matches):
                chosen_oid   = matches[idx]["oid"]
                chosen_name  = matches[idx]["name"]
                chosen_email = matches[idx]["email"]
            else:
                await ctx.yield_output(WorkflowResult(
                    status="failed",
                    message=f"Invalid number '{response}'. Pick between 1 and {len(matches)}.",
                ))
                return
        else:
            for m in matches:
                if response.strip() in (m["oid"], m["email"]):
                    chosen_oid   = m["oid"]
                    chosen_name  = m["name"]
                    chosen_email = m["email"]
                    break

        if not chosen_oid:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"'{response}' not found in results. Enter the number shown.",
            ))
            return

        await ctx.send_message(AssignRequest(
            standard=original_request.standard,
            tier=original_request.tier,
            user_oid=chosen_oid,
            user_name=chosen_name,
            user_email=chosen_email,
        ))


# ─────────────────────────────────────────────────────────────────────────────
# EXECUTOR 2: AssignUserExecutor
# ─────────────────────────────────────────────────────────────────────────────

class AssignUserExecutor(Executor):
    def __init__(self):
        super().__init__(id="assign_user")

    @handler
    async def handle(self, request: AssignRequest, ctx: WorkflowContext) -> None:
        result = await call_mcp("assign_user_to_standard", {
            "standard": request.standard,
            "user_oid": request.user_oid,
            "tier":     request.tier,
        })

        if "error" in result:
            await ctx.yield_output(WorkflowResult(
                status="failed",
                message=f"Assignment failed: {result['error']}",
            ))
            return

        if result.get("already_member"):
            await ctx.yield_output(WorkflowResult(
                status="already_done",
                message=(
                    f"{request.user_name} ({request.user_email}) is already "
                    f"a {request.tier} of '{request.standard}' — no change made."
                ),
            ))
            return

        await ctx.yield_output(WorkflowResult(
            status="success",
            message=(
                f"✓ Added {request.user_name} ({request.user_email}) "
                f"to '{request.standard}' as {request.tier}."
            ),
        ))


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_add_user_workflow():
    collector_exec = ParamCollectorExecutor()
    search_exec    = SearchUsersExecutor()
    assign_exec    = AssignUserExecutor()
    return (
        WorkflowBuilder(start_executor=collector_exec)
        .add_edge(collector_exec, search_exec)
        .add_edge(search_exec, assign_exec)
        .build()
    )


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _unlock_workflow(workflow) -> None:
    """Reset both the workflow and runner running flags after an early break."""
    workflow._reset_running_flag()
    workflow._runner._running = False


def _get_user_input(req_data) -> str:
    """Print prompt and collect input for ParamAskRequest or UserPickRequest."""
    if isinstance(req_data, ParamAskRequest):
        print(f"\n{req_data.prompt}")
        return input("> ").strip()
    elif isinstance(req_data, UserPickRequest):
        print("\nMatching users:")
        for i, m in enumerate(req_data.matches, 1):
            print(f"  {i}. {m['name']}  <{m['email']}>")
        print()
        return input("Pick a number: ").strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# WORKFLOW RUNNER
# ─────────────────────────────────────────────────────────────────────────────

async def run_add_user_workflow(standard: str, tier: str, user_query: str) -> WorkflowResult:
    workflow = build_add_user_workflow()
    request  = AddUserRequest(standard=standard, tier=tier, user_query=user_query)

    resume_id  = None
    user_input = None

    # ── First run — initial message ───────────────────────────────────────────
    async for event in workflow.run(request, stream=True):
        if event.type == "request_info":
            user_input = _get_user_input(event.data)
            resume_id  = event.request_id
            _unlock_workflow(workflow)  # reset flags before next .run() call
            break
        elif event.type == "output":
            return event.data

    if resume_id is None:
        return WorkflowResult(status="failed", message="Workflow ended without requesting input.")

    # ── Resume loop — same instance, responses only ───────────────────────────
    while True:
        got_pause = False
        async for ev in workflow.run(responses={resume_id: user_input}, stream=True):
            if ev.type == "request_info":
                user_input = _get_user_input(ev.data)
                resume_id  = ev.request_id
                _unlock_workflow(workflow)  # reset flags for next iteration
                got_pause = True
                break
            elif ev.type == "output":
                return ev.data

        if not got_pause:
            return WorkflowResult(status="failed", message="Workflow ended without output.")


# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the WFCF Standards admin assistant.

## For "add user" requests:
Extract whatever the user already mentioned from their message:
  - standard:    standard slug or name (e.g. "organic", "ISO 9001") — use "" if not mentioned
  - tier:        "user" or "admin" — use "" if not mentioned
  - user_query:  the person's name or partial email — use "" if not mentioned

DO NOT ask the user for anything. DO NOT assume missing values.
Just extract what is there and emit the JSON immediately — the system will ask for missing fields automatically.

Once intent is detected, respond with ONLY this JSON — nothing else:
{"action": "add_user", "standard": "<slug or empty>", "tier": "<tier or empty>", "user_query": "<name or empty>"}

## For other requests:
- "list standards" → call list_standards_tool, show results as a readable list.
- "list members of X" → call list_standard_members_tool(standard="X"), show name + email + tier.
- Anything else → answer helpfully.
"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=AzureCliCredential(),
    )

    agent = client.as_agent(
        name="WFCFAdmin",
        instructions=SYSTEM_PROMPT,
    )

    agent_tools = [list_standards_tool, list_standard_members_tool]

    print("WFCF Admin Assistant ready. Type 'quit' to exit.\n")

    session = agent.create_session()

    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        response = await agent.run(
            user_input,
            session=session,
            tools=agent_tools,
        )
        text = response.text.strip()

        try:
            clean  = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(clean)

            if parsed.get("action") == "add_user":
                standard   = parsed.get("standard", "").strip()
                tier       = parsed.get("tier", "").strip()
                user_query = parsed.get("user_query", "").strip()

                result = await run_add_user_workflow(standard, tier, user_query)

                if result.status == "success":
                    print(f"\n✓ {result.message}\n")
                elif result.status == "already_done":
                    print(f"\nℹ  {result.message}\n")
                else:
                    print(f"\n✗ {result.message}\n")
            else:
                print(f"Agent: {text}\n")

        except (json.JSONDecodeError, KeyError):
            print(f"Agent: {text}\n")


if __name__ == "__main__":
    asyncio.run(main())