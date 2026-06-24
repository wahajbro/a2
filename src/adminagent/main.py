"""main.py — WFCF Admin Agent entry point (server-only)

CLI mode has been removed. The router workflow IS the hosted agent —
no outer agent wrapping workflows as tools, which was the topology
behind the original 'cannot handle list[Message]' dead end.
"""

import asyncio
import os
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from agent_framework_foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from router_workflow import build_router_workflow

load_dotenv()

PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
MODEL_DEPLOYMENT = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4o")

from azure.monitor.opentelemetry import configure_azure_monitor
configure_azure_monitor(connection_string=os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"))


async def run_server():
    client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT,
        credential=DefaultAzureCredential(),
    )
    workflow = build_router_workflow(client=client, model=MODEL_DEPLOYMENT)
    agent    = workflow.as_agent(name="WFCFAdmin")
    server   = ResponsesHostServer(agent)
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(run_server())