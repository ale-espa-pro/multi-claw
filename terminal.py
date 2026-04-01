import openai
import numpy as np 
from tqdm import tqdm 
import pandas as pd 
import json 
import os
from openai import AzureOpenAI, OpenAI
import requests
from time import time
import asyncio
from dotenv import load_dotenv

from agents.agent_prompts import agent_names, datos_usuario, main_agent, system_prompts 
from tools.local_tools import total_tools, dict_total_tools
from tools.ticket_dispatcher import ticket_dispatcher
from runner.agent_runner import AgentRunner


# DATOS BASE
main_agent = "agenteTriage"
load_dotenv()
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])


# ELECION DE TOOLS DE LOS AGENTES
agent_tools = {
    "agenteTriage": ["ticket_manager_agent","web_search_agent","rag_search_agent","transfer_human"],
    "ticket_manager_agent": ["listar_tickets","modificar_ticket","eliminar_ticket"],
    "rag_search_agent": ["rag_search","web_search_agent"],
    "web_search_agent": ["web_search"]
}

agent_tools = {
    "PlannerAgent": ["ExecutorAgent"],
    "ExecutorAgent": ["WebSearchAgent", "MCPManagerAgent", "DeviceManagerAgent", "MemoryAgent"],
    "WebSearchAgent": [],
    "DeviceManagerAgent": ["read_file", "run_python", "search_files"],
    "MCPManagerAgent": ["list_mcps"],
    "MemoryAgent": ["create_cron"]
}

async def main():
    # Crear el runner
    runner = AgentRunner(
        client=client,
        system_prompts=system_prompts,
        agent_tools=agent_tools,
        dict_total_tools=dict_total_tools,
        ticket_dispatcher=ticket_dispatcher,
        main_agent="agenteTriage"
    )
    
    # Opción 1: Loop interactivo
    await runner.run_loop()
    
    # Opción 2: Uso programático
    # response = await runner.chat("Hola, necesito ayuda con mi pedido")
    # print(response)


if __name__ == "__main__":
    asyncio.run(main())











