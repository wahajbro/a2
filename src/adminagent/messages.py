"""messages.py — shared dataclasses for all workflow messages."""

#from __future__ import annotations
from dataclasses import dataclass, field


# ── Generic param collection ──────────────────────────────────────────────────

@dataclass
class ParamSpec:
    """Defines ONE required param for ParamCollectorExecutor to collect."""
    field: str          # key name in the collected dict
    prompt: str         # what to show the human if missing
    choices: list = field(default_factory=list)  # if non-empty, validate against this list


@dataclass
class CollectedParams:
    """Filled-in params forwarded from ParamCollectorExecutor to the next executor."""
    params: dict        # {"standard": "organic", "tier": "user", ...}


@dataclass
class ParamAskRequest:
    """Sent via request_info when a param is missing — human must fill it."""
    field: str
    prompt: str
    choices: list       # shown as numbered list if non-empty
    current: dict       # everything collected so far
    remaining_specs: list  # list of ParamSpec still to collect after this one


# ── User search / pick ────────────────────────────────────────────────────────

@dataclass
class UserPickRequest:
    """Sent via request_info when search returns matches — human must pick one."""
    matches: list       # [{name, email, oid}, ...]
    carry: dict         # params to carry forward after pick (standard, tier, slug, etc.)


@dataclass
class UserPickResult:
    oid: str
    name: str
    email: str
    carry: dict


# ── Confirmation ──────────────────────────────────────────────────────────────

@dataclass
class ConfirmRequest:
    """Sent via request_info for destructive or irreversible actions."""
    message: str        # "Are you sure you want to delete X? (yes/no)"
    carry: dict


# ── Final result ──────────────────────────────────────────────────────────────

@dataclass
class WorkflowResult:
    status: str         # "success" | "failed" | "already_done" | "cancelled"
    message: str
    warnings: list = field(default_factory=list)
