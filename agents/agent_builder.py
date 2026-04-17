import json
import os
import time
from dotenv import load_dotenv
from agents.agent_prompts import base_prompts
from tools.local_tools import dict_total_tools
from tools.ticket_dispatcher import ticket_dispatcher

load_dotenv()

_DATOS_USUARIO = {
    "system": os.environ.get("USER_SYSTEM"),
    "telefono": os.environ.get("USER_PHONE"),
    "correo_principal": os.environ.get("USER_EMAIL"),
    "windows_path1": os.environ.get("USER_WINDOWS_PATH1"),
    "windows_path2": os.environ.get("USER_WINDOWS_PATH2"),
}

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "agent_config.json")


class AgentBuilder:
    def __init__(self, working_base: str | None = None):
        self.working_base = working_base or os.environ.get("WORKING_PATH", "/tmp/planner/")
        self._agents: dict[str, dict] = {}
        self.main_agent: str | None = None
        self.dict_total_tools = dict_total_tools
        self.ticket_dispatcher = ticket_dispatcher

    # ── Registration ──

    def register(
        self,
        name: str,
        base_prompt: str,
        tools: list[str],
        main: bool = False,
        json_response: bool = False,
    ):
        self._agents[name] = {
            "base_prompt": base_prompt,
            "tools": tools,
            "json_response": json_response,
        }
        if main:
            self.main_agent = name

    def load_agents(self, config_path: str | None = None):
        """Register all agents from JSON config + base_prompts."""
        config_path = config_path or _CONFIG_PATH
        with open(config_path) as f:
            configs = json.load(f)

        for name, cfg in configs.items():
            if name not in base_prompts:
                continue
            self.register(
                name=name,
                base_prompt=base_prompts[name],
                tools=cfg.get("tools", []),
                main=cfg.get("main", False),
                json_response=cfg.get("json_response", False),
            )

    # ── Properties for AgentRunner ──

    @property
    def agent_names(self) -> set[str]:
        return set(self._agents.keys())

    @property
    def agent_tools(self) -> dict[str, list[str]]:
        return {name: cfg["tools"] for name, cfg in self._agents.items()}

    def uses_json_response(self, agent_name: str) -> bool:
        return self._agents[agent_name]["json_response"]

    def get_tools_for_agent(self, agent_name: str) -> list[dict]:
        """Return tool schemas for an agent's tool list."""
        return [self.dict_total_tools[name] for name in self._agents[agent_name]["tools"]]

    # ── System prompt building ──

    def build_system_prompt(
        self,
        agent_name: str,
        session_id: str,
        conversation_type: str | None = None,
    ) -> str:
        """Build full system prompt = base + preferences + working dir."""
        cfg = self._agents[agent_name]
        parts = [
            cfg["base_prompt"],
            self._preferences_section(),
            self._working_dir_section(session_id, conversation_type),
            self._crons_section(),
            self._worflows_section()
        ]
        return "\n\n".join(p for p in parts if p)

    def build_all_prompts(
        self,
        session_id: str,
        conversation_type: str | None = None,
    ) -> dict[str, str]:
        """Build system prompts for all registered agents."""
        return {
            name: self.build_system_prompt(name, session_id, conversation_type)
            for name in self._agents
        }

    # ── Prompt sections ──

    def _list_directories(self, path: str) -> str:
        result = []
        for entry in os.scandir(path):
            stat = entry.stat()
            name = entry.name
            size = stat.st_size
            modified = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
            created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_ctime))
            result.append(f"{name} | size: {size} | created: {created} | modified: {modified}")
        return str(result)

    def _worflows_section(self) -> str:
        path = os.environ.get("WORKFLOW_PATH")
        if not path or not os.path.isdir(path):
            return ""
        workflow_names = self._list_directories(path)
        return f"Workflows creados: \n -{workflow_names} \n"

    def _crons_section(self) -> str:
        path = os.environ.get("CRONS_PATH")
        if not path or not os.path.isdir(path):
            return ""
        cron_list = self._list_directories(path)
        return f"Agentes Cron programados: \n -{cron_list} \n"

    def _programing_tasks(self) -> str:

        return f"Agentes Cron programados: \n -{cron_list} \n"

    @staticmethod
    def _preferences_section() -> str:
        path = os.environ.get("USER_PREFERENCES_PATH")
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r") as f:
            prefs = f.read().strip()
        if not prefs:
            return ""
        return (
            "## Preferencias del usuario ##\n"
            f"{prefs}\n\n"
            "Se podrán añadir preferencias con la herramienta save_preference.\n"
            "Otros metadatos del usuario: " + str(_DATOS_USUARIO)
        )

    def _working_dir_section(self, session_id: str, conversation_type: str | None) -> str:
        """Resolve working directory, create it if needed, return prompt section."""
        if conversation_type == "temporal":
            return "Esta conversación es temporal. No se almacenará memoria ni archivos."

        if conversation_type == "cron":
            base_dir = os.path.join(self.working_base, "crons")
        else:
            base_dir = os.path.join(self.working_base, "sessions")

        working_dir = os.path.join(base_dir, session_id)
        os.makedirs(working_dir, exist_ok=True)

        return (
            "## Entorno de ejecución ##\n"
            "Todas los directorios de conversaciones e interacciones creadas a lo largo de la historia viven en /home/ale/multi-claw/sessions/*.\n"
            "cada carpeta tiene asigando como nombre un conversation_id que está asociado en la DB de cada conversación previa con agentes \n"
            "dentro de /home/ale/multi-claw/cron-agents se encuentran todas los directorios de tareas programadas de agentes con sus archivos, resultados etc \n"
            "dentro de /home/ale/multi-claw/workflows se encuentran todos los flujos de trabajo creados por agentes similar a agent-crons pero estas se ejecutan cuando el usuario lo solicita. \n"
            f"**Directorio de trabajo: {working_dir}**\n"
            "Cualquier archivo, datos o resultados de la sesión actual se almacenará por defecto en esta carpeta con subcarpetas si es necesario. En caso de que sea necesario siempre se podra modificar cualquier otros directorios mientras esté permitido"
        )
