"""Enjambre de agentes efímeros con presupuesto y carpeta asignada.

El `agent_id` de un nodo es su ruta relativa dentro del árbol, así que la
jerarquía de agentes, la de directorios y la de carriles de `context_jsonb` son
la misma cosa: el inspector reconstruye el árbol sin estado adicional.

Presupuesto: `budget` es el número total de agentes del enjambre, raíz incluida.
Crear un hijo con `budget=k` cuesta `1+k` al padre (su plaza más lo que le cede)
y lo que el hijo no gasta vuelve al padre al terminar. La invariante resultante
es que nunca se crean más agentes que el presupuesto inicial, y `max_budget`
acota la suma de todos los enjambres de una misma petición.
"""

import asyncio
import os
import re
import unicodedata
from dataclasses import dataclass, field

from agents.agent_prompts import SWARM_WORKER_PROMPT
from app_paths import get_allowed_write_roots
from runner.sandbox import is_within

REPORT_FILENAME = "REPORT.md"
REPORT_PLACEHOLDER = "<!-- Informe pendiente: sustituye este contenido por tu informe. -->\n"
# Turno extra que se le concede a un nodo que terminó sin dejar informe.
REPORT_NUDGE_ITERATIONS = 4

REPORT_NUDGE = (
    f"[SYSTEM] No has escrito {REPORT_FILENAME} en tu carpeta de trabajo y es obligatorio: "
    "el agente que te invocó no puede ver nada de lo que has hecho sin él. "
    "Escríbelo ahora con run_command o, si no tienes terminal, completa el archivo inicial "
    "con edit_file (inspect y edit), respetando la plantilla de secciones. Responde solo "
    "cuando esté guardado."
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class SwarmError(Exception):
    """Error de contrato del enjambre; se devuelve al modelo como salida de tool."""


def slugify(name: str, fallback: str = "agente") -> str:
    # Los nombres llegan en castellano: sin plegar los acentos, "investigación"
    # acabaría como "investigaci-n" y el nombre de carpeta sería ilegible.
    folded = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    slug = _SLUG_RE.sub("-", folded.strip().lower()).strip("-")
    return slug[:48] or fallback


@dataclass
class SwarmNode:
    agent_id: str
    parent_id: str | None
    root_path: str
    task: str
    depth: int
    budget: int
    granted: int
    extra_prompt: str = ""
    model: str | None = None
    max_iterations: int = 25
    allow_shell: bool = True
    status: str = "running"
    spawned: list[str] = field(default_factory=list)

    @property
    def report_path(self) -> str:
        return os.path.join(self.root_path, REPORT_FILENAME)


def _resolve(path: str) -> str:
    return os.path.realpath(os.path.expandvars(os.path.expanduser(str(path))))


class SwarmRegistry:
    """Contabilidad de nodos y presupuesto para un único request.

    Vive en el ExecutionContext, de modo que dos sesiones concurrentes no
    comparten ni presupuesto ni identidades.
    """

    def __init__(self, config: dict, taken_ids: set[str] | None = None):
        self.config = dict(config)
        self.nodes: dict[str, SwarmNode] = {}
        self.roots: list[str] = []
        self._committed_budget = 0
        self._taken_ids = taken_ids if taken_ids is not None else set()
        self._lock = asyncio.Lock()
        self.request_semaphore = asyncio.Semaphore(max(int(self.config.get("max_concurrent", 6)), 1))

    def get(self, agent_id: str) -> SwarmNode | None:
        return self.nodes.get(agent_id)

    @property
    def report_max_chars(self) -> int:
        return max(int(self.config.get("report_max_chars", 20_000)), 1_000)

    # ── Identidad ──

    def _claim_id(self, candidate: str) -> str:
        """Reserva un agent_id libre.

        Una carpeta puede reutilizarse entre turnos (reiterar sobre el trabajo
        anterior), pero el carril de contexto no: se sufija para que el trace
        conserve cada ejecución por separado.
        """
        agent_id = candidate
        attempt = 2
        while agent_id in self._taken_ids or agent_id in self.nodes:
            agent_id = f"{candidate}#{attempt}"
            attempt += 1
        self._taken_ids.add(agent_id)
        return agent_id

    # ── Creación de nodos ──

    def _validate_root_path(self, path: str) -> str:
        real_path = _resolve(path)
        if not any(is_within(real_path, root) for root in get_allowed_write_roots()):
            raise SwarmError(f"root_path fuera de las rutas permitidas: {get_allowed_write_roots()}")
        if os.path.isfile(real_path):
            raise SwarmError(f"root_path apunta a un archivo existente: {real_path}")
        return real_path

    async def create_root(
        self,
        *,
        root_path: str,
        budget: int,
        task: str,
        name: str | None = None,
        extra_prompt: str = "",
        model: str | None = None,
        max_iterations: int | None = None,
        allow_shell: bool = True,
    ) -> SwarmNode:
        if not isinstance(allow_shell, bool):
            raise SwarmError("allow_shell debe ser un booleano")
        max_budget = int(self.config.get("max_budget", 50))
        try:
            budget = int(budget)
        except (TypeError, ValueError):
            raise SwarmError(f"budget debe ser un entero entre 1 y {max_budget}")
        if not 1 <= budget <= max_budget:
            raise SwarmError(f"budget debe estar entre 1 y {max_budget} (recibido: {budget})")
        if not str(task or "").strip():
            raise SwarmError("task es obligatorio: es el mensaje de usuario del agente raíz")

        real_root = self._validate_root_path(root_path)

        async with self._lock:
            available = max_budget - self._committed_budget
            if budget > available:
                raise SwarmError(
                    f"presupuesto de la petición insuficiente: quedan {available} de {max_budget} agentes"
                )
            self._committed_budget += budget
            agent_id = self._claim_id(slugify(name or os.path.basename(real_root), "swarm"))
            node = SwarmNode(
                agent_id=agent_id,
                parent_id=None,
                root_path=real_root,
                task=str(task),
                depth=0,
                # La raíz ocupa una plaza del presupuesto total.
                budget=budget - 1,
                granted=budget - 1,
                extra_prompt=str(extra_prompt or ""),
                model=model,
                max_iterations=self._resolve_max_iterations(max_iterations),
                allow_shell=bool(allow_shell),
            )
            self.nodes[agent_id] = node
            self.roots.append(agent_id)

        os.makedirs(real_root, exist_ok=True)
        _prepare_report(node)
        return node

    async def spawn(
        self,
        *,
        parent_id: str,
        name: str,
        task: str,
        budget: int = 0,
        extra_prompt: str = "",
        subpath: str | None = None,
        model: str | None = None,
        max_iterations: int | None = None,
        allow_shell: bool | None = None,
    ) -> SwarmNode:
        if allow_shell is not None and not isinstance(allow_shell, bool):
            raise SwarmError("allow_shell debe ser un booleano o null para heredar el acceso")
        parent = self.nodes.get(parent_id)
        if parent is None:
            raise SwarmError(
                "spawn_agent solo se puede usar desde dentro de un enjambre; "
                "el agente raíz se crea con deploy_swarm"
            )
        if not str(task or "").strip():
            raise SwarmError("task es obligatorio: es el mensaje de usuario del subagente")

        try:
            budget = max(int(budget or 0), 0)
        except (TypeError, ValueError):
            raise SwarmError("budget debe ser un entero >= 0")

        max_depth = int(self.config.get("max_depth", 4))
        if parent.depth + 1 > max_depth:
            raise SwarmError(
                f"profundidad máxima del árbol alcanzada ({max_depth}); resuelve la tarea en este nivel"
            )
        if allow_shell is None:
            allow_shell = parent.allow_shell
        if allow_shell and not parent.allow_shell:
            raise SwarmError("no puedes conceder allow_shell: tu propia rama no tiene terminal")

        slug = slugify(name)
        child_path = _resolve(os.path.join(parent.root_path, str(subpath or slug)))
        if child_path == parent.root_path or not is_within(child_path, parent.root_path):
            raise SwarmError(
                f"la carpeta del subagente debe ser una subcarpeta de {parent.root_path}"
            )

        async with self._lock:
            cost = 1 + budget
            if parent.budget < cost:
                raise SwarmError(
                    f"presupuesto insuficiente: te quedan {parent.budget} y crear un agente con "
                    f"budget={budget} cuesta {cost} (su plaza + lo que le cedes)"
                )
            parent.budget -= cost
            agent_id = self._claim_id(f"{parent.agent_id}/{slug}")
            node = SwarmNode(
                agent_id=agent_id,
                parent_id=parent.agent_id,
                root_path=child_path,
                task=str(task),
                depth=parent.depth + 1,
                budget=budget,
                granted=budget,
                extra_prompt=str(extra_prompt or ""),
                model=model or parent.model,
                max_iterations=self._resolve_max_iterations(max_iterations),
                allow_shell=bool(allow_shell),
            )
            self.nodes[agent_id] = node
            parent.spawned.append(agent_id)

        os.makedirs(child_path, exist_ok=True)
        _prepare_report(node)
        return node

    def _resolve_max_iterations(self, requested: int | None) -> int:
        default = int(self.config.get("max_iterations", 25))
        if requested is None:
            return default
        try:
            return max(min(int(requested), default * 2), 1)
        except (TypeError, ValueError):
            return default

    async def settle(self, node: SwarmNode, status: str) -> int:
        """Cierra un nodo y devuelve al padre el presupuesto que no gastó."""
        async with self._lock:
            node.status = status
            returned = node.budget
            node.budget = 0
            parent = self.nodes.get(node.parent_id) if node.parent_id else None
            if parent is not None:
                parent.budget += returned
            else:
                self._committed_budget -= returned
        return returned


# ── Prompt del worker ──


def _prepare_report(node: SwarmNode) -> None:
    """Deja un archivo editable cuando la rama no dispone de terminal."""
    if node.allow_shell:
        return
    try:
        with open(node.report_path, "x", encoding="utf-8") as report_file:
            report_file.write(REPORT_PLACEHOLDER)
    except FileExistsError:
        # Al reiterar una carpeta, conserva el informe que dejó el turno previo.
        pass


def _environment_section(node: SwarmNode, max_depth: int) -> str:
    shell = "sí (sin aislamiento de escritura por carpetas)" if node.allow_shell else "no disponible"
    write_policy = (
        "Guarda los artefactos en esta carpeta. La terminal puede escribir fuera de ella; "
        "el control de rutas de edit_file no limita los comandos de shell.\n"
        if node.allow_shell
        else "edit_file solo permite modificar archivos dentro de esta carpeta. Puedes inspeccionar "
        "fuera de ella. Completa el REPORT.md existente mediante inspect y edit.\n"
    )
    if node.budget:
        budget_line = (
            f"Presupuesto de subagentes: {node.budget}. Crear uno con budget=k te cuesta 1+k; "
            "lo que no gaste te será devuelto al terminar."
        )
    else:
        budget_line = "Presupuesto de subagentes: 0. Resuelve la tarea tú mismo, no puedes delegar."

    return (
        "## Tu entorno ##\n"
        f"agent_id: {node.agent_id}\n"
        f"**Carpeta de trabajo: {node.root_path}**\n"
        f"{write_policy}"
        f"Profundidad: {node.depth} de {max_depth}\n"
        f"{budget_line}\n"
        f"run_command: {shell}\n"
        f"Informe obligatorio: {node.report_path}"
    )


def build_worker_prompt(node: SwarmNode, max_depth: int) -> str:
    sections = [SWARM_WORKER_PROMPT.strip()]
    extra = str(node.extra_prompt or "").strip()
    if extra:
        sections.append(f"## Instrucciones específicas de tu rama ##\n{extra}")
    sections.append(_environment_section(node, max_depth))
    return "\n\n".join(sections)


def read_report(node: SwarmNode, max_chars: int) -> dict | None:
    """Lee el REPORT.md del nodo para devolvérselo al padre ya en el resultado."""
    path = node.report_path
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as report_file:
            content = report_file.read(max_chars + 1)
    except OSError as exc:
        return {"report_path": path, "report_error": str(exc)}

    if not content.strip() or content.strip() == REPORT_PLACEHOLDER.strip():
        return None

    truncated = len(content) > max_chars
    result = {"report_path": path, "report": content[:max_chars]}
    if truncated:
        result["report_truncated"] = True
        result["report_hint"] = (
            "Informe recortado; usa run_command o edit_file action=inspect con offset y max_chars "
            "sobre report_path para leer los fragmentos que necesites."
        )
    return result
