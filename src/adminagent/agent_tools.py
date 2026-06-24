"""agent_tools.py — shared read-only MCP-backed tools.
Used by both CLI mode (main.py) and the server router workflow so there's
exactly one definition instead of two copies drifting apart.
"""
import json
from mcp_client import call_mcp


async def list_standards_tool() -> str:
    """List all available WFCF standards."""
    result = await call_mcp("list_standards", {})
    if "error" in result:
        return f"Error: {result['error']}"
    standards = result.get("standards", [])
    if not standards:
        return "No standards found."
    return json.dumps([
        {"name": s["name"], "display_name": s.get("display_name", s["name"])}
        for s in standards
    ])


async def list_standard_members_tool(standard: str) -> str:
    """List current members of a standard."""
    if not standard or len(standard) < 2:
        return "Error: Please provide a valid standard name (e.g. 'organic')."
    result = await call_mcp("list_standard_members", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    return json.dumps([
        {"name": m.get("display_name", ""), "email": m.get("email", ""), "tier": m.get("tier", "")}
        for m in members
    ])


async def list_subgroups_tool(standard: str) -> str:
    """List subgroups of a standard."""
    result = await call_mcp("list_subgroups", {"standard": standard})
    if "error" in result:
        return f"Error: {result['error']}"
    sgs = result.get("subgroups", [])
    return json.dumps([
        {"slug": s["slug"], "display_name": s.get("display_name", s["slug"])}
        for s in sgs
    ])


async def list_subgroup_members_tool(standard: str, slug: str) -> str:
    """List members of a subgroup."""
    result = await call_mcp("list_subgroup_members", {"standard": standard, "slug": slug})
    if "error" in result:
        return f"Error: {result['error']}"
    members = result.get("members", [])
    if not members:
        return f"No members in subgroup '{slug}'."
    return json.dumps([
        {"name": m.get("display_name", ""), "email": m.get("email", "")}
        for m in members
    ])


async def list_subgroup_paths_tool(standard: str, slug: str) -> str:
    """List granted paths for a subgroup."""
    result = await call_mcp("list_subgroup_paths", {"standard": standard, "slug": slug})
    if "error" in result:
        return f"Error: {result['error']}"
    paths = result.get("paths", [])
    if not paths:
        return f"No paths granted to '{slug}'."
    return "\n".join(paths)


async def search_documents_tool(query: str, standard_family: str = "") -> str:
    """Search regulatory documents."""
    args = {"query": query}
    if standard_family:
        args["standard_family"] = standard_family
    result = await call_mcp("search_documents", args)
    if "error" in result:
        return f"Error: {result['error']}"
    chunks = result.get("chunks", [])
    if not chunks:
        return "No documents found."
    return json.dumps([
        {"title": c.get("title", ""), "section": c.get("section", ""),
         "content": c.get("content", "")[:300], "path": c.get("source_path", "")}
        for c in chunks
    ])
async def search_users_tool(query: str) -> str:
    """Find a user by OID, email/UPN, or name prefix. Use for 'who is X' / 'find user X' questions."""
    result = await call_mcp("search_users", {"query": query})
    if "error" in result:
        return f"Error: {result['error']}"
    users = result.get("users", [])
    if not users:
        return f"No user found matching '{query}'."
    return json.dumps([
        {"name": u.get("display_name", ""), "email": u.get("email", ""), "oid": u.get("oid", "")}
        for u in users
    ])

AGENT_TOOLS = [
    list_standards_tool,
    list_standard_members_tool,
    list_subgroups_tool,
    list_subgroup_members_tool,
    list_subgroup_paths_tool,
    search_documents_tool,
    search_users_tool,
]