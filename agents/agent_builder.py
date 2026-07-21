import json
import os
import re
import time
from dotenv import load_dotenv
from app_paths import (
    get_crons_path,
    get_sessions_path,
    get_user_preferences_path,
    get_workflows_path,
    get_working_path,
)
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
README_DESCRIPTION_MAX_CHARS = 20_000
README_DESCRIPTION_MAX_WORDS = 100

DEFAULT_AGENT_PARAMS = {
    "model": "gpt-5.5",
    "provider": None,  # None = inferido por el prefijo del modelo (claude-* -> anthropic)
    "reasoning": {"effort": "medium", "summary": "auto"},
    "parallel_tool_calls": False,
    "max_iterations": 10,
    "max_output_tokens": 16_000,  # solo lo usa el provider de Anthropic (max_tokens obligatorio)
}
DEFAULT_RUNNER_CONFIG = {
    "max_iterations": 400,
}


class AgentBuilder:
    def __init__(self, working_base: str | None = None):
        self._working_base_override = working_base is not None
        self.working_base = working_base or get_working_path()
        self._agents: dict[str, dict] = {}
        self._defaults: dict = dict(DEFAULT_AGENT_PARAMS)
        self._runner_config: dict = dict(DEFAULT_RUNNER_CONFIG)
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
        model: str | None = None,
        provider: str | None = None,
        reasoning: dict | None = None,
        parallel_tool_calls: bool | None = None,
        max_iterations: int | None = None,
        max_output_tokens: int | None = None,
        text: dict | None = None,
    ):
        merged_reasoning = {
            **dict(self._defaults.get("reasoning") or {}),
            **dict(reasoning or {}),
        }
        if text is None and json_response:
            text = {"format": {"type": "json_object"}}

        self._agents[name] = {
            "base_prompt": base_prompt,
            "tools": tools,
            "json_response": json_response,
            "model": model or self._defaults.get("model"),
            "provider": provider or self._defaults.get("provider"),
            "reasoning": merged_reasoning,
            "parallel_tool_calls": (
                bool(parallel_tool_calls)
                if parallel_tool_calls is not None
                else bool(self._defaults.get("parallel_tool_calls", False))
            ),
            "max_iterations": (
                int(max_iterations)
                if max_iterations is not None
                else int(self._defaults.get("max_iterations", 10))
            ),
            "max_output_tokens": int(
                max_output_tokens
                if max_output_tokens is not None
                else self._defaults.get("max_output_tokens", 16_000)
            ),
            "text": text,
        }
        if main:
            self.main_agent = name

    def load_agents(self, config_path: str | None = None):
        """Register all agents from JSON config + base_prompts."""
        config_path = config_path or _CONFIG_PATH
        with open(config_path) as f:
            configs = json.load(f)

        self._runner_config = {
            **DEFAULT_RUNNER_CONFIG,
            **dict(configs.get("_runner") or {}),
        }
        self._defaults = {
            **DEFAULT_AGENT_PARAMS,
            **dict(configs.get("_defaults") or {}),
        }
        self._defaults["reasoning"] = {
            **dict(DEFAULT_AGENT_PARAMS["reasoning"]),
            **dict((configs.get("_defaults") or {}).get("reasoning") or {}),
        }

        for name, cfg in configs.items():
            if name.startswith("_"):
                continue
            if name not in base_prompts:
                continue
            self.register(
                name=name,
                base_prompt=base_prompts[name],
                tools=cfg.get("tools", []),
                main=cfg.get("main", False),
                json_response=cfg.get("json_response", False),
                model=cfg.get("model"),
                provider=cfg.get("provider"),
                reasoning=cfg.get("reasoning"),
                parallel_tool_calls=cfg.get("parallel_tool_calls"),
                max_iterations=cfg.get("max_iterations"),
                max_output_tokens=cfg.get("max_output_tokens"),
                text=cfg.get("text"),
            )

    # ── Properties for AgentRunner ──

    @property
    def agent_names(self) -> set[str]:
        return set(self._agents.keys())

    def get_runner_config(self) -> dict:
        return dict(self._runner_config)

    def get_agent_max_iterations(self, agent_name: str) -> int:
        return int(self._agents[agent_name]["max_iterations"])

    def get_agent_params(self, agent_name: str) -> dict:
        """Parámetros de modelo del agente; cada provider mapea los suyos."""
        cfg = self._agents[agent_name]
        return {
            "model": cfg["model"],
            "provider": cfg["provider"],
            "reasoning": cfg["reasoning"],
            "parallel_tool_calls": cfg["parallel_tool_calls"],
            "max_output_tokens": cfg["max_output_tokens"],
            "text": cfg.get("text"),
        }

    def get_tools_for_agent(self, agent_name: str) -> list[dict]:
        """Return tool schemas for an agent's tool list."""
        tools = []
        for tool in self._agents[agent_name]["tools"]:
            if isinstance(tool, dict):
                tools.append(tool)
            else:
                tools.append(self.dict_total_tools[tool])
        return tools

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
            self._workflows_section()
        ]
        return "\n\n".join(p for p in parts if p)

    # ── Prompt sections ──

    @staticmethod
    def _clip_words(text: str, max_words: int = README_DESCRIPTION_MAX_WORDS) -> str:
        words = text.split()
        if len(words) <= max_words:
            return " ".join(words)
        return " ".join(words[:max_words])

    def _read_readme_description(self, directory_path: str) -> str | None:
        for filename in ("README.md", "readme.md"):
            readme_path = os.path.join(directory_path, filename)
            if not os.path.isfile(readme_path):
                continue

            try:
                with open(readme_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(README_DESCRIPTION_MAX_CHARS)
            except OSError:
                return None

            match = re.search(r"(?im)^\s*description\s*:\s*(.+?)\s*$", content)
            if not match:
                return None

            description = self._clip_words(match.group(1).strip())
            return description or None

        return None

    def _list_task_directories(self, path: str) -> str:
        lines = []
        for entry in sorted(os.scandir(path), key=lambda item: item.name.casefold()):
            if not entry.is_dir(follow_symlinks=False):
                continue

            stat = entry.stat()
            name = entry.name
            description = self._read_readme_description(entry.path) or "sin descripcion en README.md"
            modified = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
            created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_ctime))
            lines.append(
                "\n".join(
                    [
                        f"- nombre: {name}",
                        f"  ruta: {entry.path}",
                        f"  description: {description}",
                        f"  created: {created}",
                        f"  modified: {modified}",
                    ]
                )
            )
        return "\n".join(lines)

    def _workflows_section(self) -> str:
        path = get_workflows_path(self.working_base) if self._working_base_override else get_workflows_path()
        if not path or not os.path.isdir(path):
            return ""
        workflow_names = self._list_task_directories(path)
        if not workflow_names:
            return ""
        return f"Workflows creados:\n{workflow_names}\n"

    def _crons_section(self) -> str:
        path = get_crons_path(self.working_base) if self._working_base_override else get_crons_path()
        if not path or not os.path.isdir(path):
            return ""
        cron_list = self._list_task_directories(path)
        if not cron_list:
            return ""
        return f"Agentes Cron programados:\n{cron_list}\n"

    @staticmethod
    def _preferences_section() -> str:
        path = get_user_preferences_path()
        if not path or not os.path.exists(path):
            return ""
        with open(path, "r") as f:
            prefs = f.read().strip()
        if not prefs:
            return ""
        return (
            "## Preferencias del usuario ##\n"
            f"{prefs}\n\n"
            "Se podrán añadir, modificar o eliminar preferencias con la herramienta save_preference.\n"
            "Otros metadatos del usuario: " + str(_DATOS_USUARIO)
        )

    def _working_dir_section(self, session_id: str, conversation_type: str | None) -> str:
        """Resolve working directory, create it if needed, return prompt section."""
        if conversation_type == "temporal":
            return "Esta conversación es temporal. No se almacenará memoria ni archivos."

        if conversation_type == "cron":
            base_dir = get_crons_path(self.working_base) if self._working_base_override else get_crons_path()
        else:
            base_dir = get_sessions_path(self.working_base)

        working_dir = os.path.join(base_dir, session_id)
        os.makedirs(working_dir, exist_ok=True)
        sessions_path = get_sessions_path(self.working_base)
        crons_path = get_crons_path(self.working_base) if self._working_base_override else get_crons_path()
        workflows_path = get_workflows_path(self.working_base) if self._working_base_override else get_workflows_path()

        return (
            "## Entorno de ejecución ##\n"
            f"Raíz de trabajo: {self.working_base}\n"
            f"Sesiones: {sessions_path}\n"
            f"Crons: {crons_path}\n"
            f"Workflows: {workflows_path}\n"
            "Cada carpeta de sesión usa un conversation_id asociado a la base de datos.\n"
            f"**Directorio de trabajo: {working_dir}**\n"
            "Cualquier archivo, dato o resultado de la sesión actual se almacenará por defecto en esta carpeta con subcarpetas si es necesario."
        )
