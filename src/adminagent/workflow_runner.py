"""workflow_runner.py — generic runner for any MAF workflow with multiple request_info pauses.

Handles:
  - ParamAskRequest  → prints prompt, collects input
  - UserPickRequest  → prints numbered list, collects pick
  - ConfirmRequest   → prints message, collects yes/no
  - WorkflowResult   → returns final result

No workflow-specific logic here — just the event loop.
"""

# from __future__ import annotations
from messages import ParamAskRequest, UserPickRequest, ConfirmRequest, WorkflowResult


def _unlock(workflow) -> None:
    """Reset MAF running flags after an early break so .run() can be called again."""
    try:
        workflow._reset_running_flag()
    except Exception:
        pass
    try:
        workflow._runner._running = False
    except Exception:
        pass

def _get_human_input(req_data) -> str:
    """Print the appropriate prompt and return stripped human input."""
    if isinstance(req_data, ParamAskRequest):
        print(f"\n{req_data.prompt}")
        return input("> ").strip()

    elif isinstance(req_data, UserPickRequest):
        print("\nMatching users:")
        for i, m in enumerate(req_data.matches, 1):
            print(f"  {i}. {m['name']}  <{m['email']}>")
        print()
        return input("Pick a number: ").strip()

    elif isinstance(req_data, ConfirmRequest):
        print(f"\n{req_data.message}")
        return input("> ").strip()

    return ""


async def run_workflow(workflow, initial_message) -> WorkflowResult:
    """Run a workflow to completion, handling all request_info pauses inline."""
    resume_id  = None
    user_input = None

    # ── First run ─────────────────────────────────────────────────────────────
    async for event in workflow.run(initial_message, stream=True):
        if event.type == "request_info":
            user_input = _get_human_input(event.data)
            resume_id  = event.request_id
            _unlock(workflow)
            break
        elif event.type == "output":
            return event.data

    if resume_id is None:
        return WorkflowResult(status="failed", message="Workflow ended without requesting input.")

    # ── Resume loop ───────────────────────────────────────────────────────────
    while True:
        got_pause = False
        async for ev in workflow.run(responses={resume_id: user_input}, stream=True):
            if ev.type == "request_info":
                user_input = _get_human_input(ev.data)
                resume_id  = ev.request_id
                _unlock(workflow)
                got_pause = True
                break
            elif ev.type == "output":
                return ev.data

        if not got_pause:
            return WorkflowResult(status="failed", message="Workflow ended without output.")
