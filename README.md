# Multi-Claw

Multi-Claw es un backend experimental de orquestacion multiagente con FastAPI, OpenAI Responses API, memoria persistente en PostgreSQL y cache de contexto en Redis.

El proyecto esta orientado a uso personal/local, pero ya incluye piezas para funcionar como servicio: API HTTP, UI HTML simple, webhook de Twilio/WhatsApp, herramientas locales de archivos/comandos, memoria semantica por embeddings y busqueda textual.

## Por Que Es Interesante

Multi-Claw explora una idea simple: que un asistente local sea mas util si no depende de un unico hilo gigante de contexto, sino de una red de agentes especializados, memoria consultable y workflows reutilizables.

Lo mas potente del proyecto:

- **Agentes autoprogramables:** se pueden crear tareas autonomas periodicas, tipo cron, donde uno o varios agentes se autoinvocan, revisan estado, producen artefactos y dejan trazabilidad.
- **Menos context rotting:** la arquitectura multiagente reparte responsabilidades para evitar que una sola conversacion se degrade al crecer.
- **Control de costes:** permite delegar subtareas a subagentes mas baratos o con menor razonamiento cuando no hace falta usar el modelo mas potente para todo.
- **Mejor aislamiento ante prompt injection:** no es una defensa determinista, pero separar agentes, contratos, herramientas y memoria reduce parte de la superficie frente a mezclarlo todo en un unico contexto.
- **Memoria de acciones completas:** la memoria no solo guarda mensajes; tambien puede recuperar acciones, outputs de herramientas, subagentes, contratos entre agentes, rutas, estados, errores y decisiones.
- **Retrieval complejo y multitemporal:** el sistema puede buscar por sesiones, subagentes, tipo de evento, chunks semanticos, texto literal, ventanas temporales y fallback a `context_jsonb`.
- **Workflows como habilidades reutilizables:** los workflows guardan playbooks, prompts y plantillas para que futuros agentes repitan procesos con contexto operativo.
- **Preferencias de usuario persistentes:** el sistema puede inyectar preferencias y metadatos personales de forma controlada cuando estan configurados.

En pruebas internas de uso real, el enfoque de memoria ha dado un recall muy alto en corpus conversacionales grandes, incluso por encima de 20 millones de tokens. Falta convertir esa observacion en benchmarks formales, asi que debe leerse como una direccion prometedora, no como una garantia medida.

## Estado Real

Este repositorio no es todavia un producto empaquetado. Estas son las realidades importantes:

- Requiere `OPENAI_API_KEY` para ejecutar agentes y generar embeddings.
- Requiere PostgreSQL para persistencia de conversaciones.
- Redis se usa como cache de contexto y para rate limits/idempotencia de Twilio cuando esta disponible.
- La busqueda vectorial usa `pgvector` si la extension esta disponible; si no, los embeddings se guardan como JSONB y no hay busqueda vectorial.
- No se usa Qdrant en runtime.
- Twilio es opcional, pero el router esta incluido en la app.
- El sandbox de `run_python` usa APIs Linux (`resource`, `prctl`), asi que esta pensado para Linux/WSL/Docker.
- Las rutas de trabajo salen de variables de entorno y, si no se configuran, usan `./working-dir` dentro del clone local.
- Las herramientas locales pueden leer archivos y ejecutar comandos segun la configuracion del agente; no debe exponerse en Internet sin revisar seguridad.

## Arranque Rapido Para Un Clone Nuevo

El camino recomendado para otra persona es Docker Compose.

1. Clonar el repositorio:

```bash
git clone <url-del-repo>
cd multi-claw
```

2. Definir la clave de OpenAI:

```bash
export OPENAI_API_KEY="sk-..."
```

3. Levantar la app:

```bash
docker compose up --build
```

4. Abrir la UI:

```text
http://localhost:8000
```

Con Docker no hace falta instalar PostgreSQL, Redis ni Python localmente. Docker crea:

- `app`: backend FastAPI y UI.
- `db`: PostgreSQL con pgvector.
- `redis`: cache y rate limits.
- `planner-workspace`: volumen interno montado en `/tmp/planner`.

Para uso local sin Docker, ver "Arranque Local Sin Docker".

## Que Debe Configurar Cada Persona

Obligatorio:

- `OPENAI_API_KEY`: clave de OpenAI.

Recomendado para uso privado/local:

- `CHAT_API_KEY`: protege `/chat`, `/conversations` y endpoints de borrado con header `X-API-Key`.
- `WORKING_PATH`: carpeta donde se guardan sesiones, crons, workflows, capturas y preferencias. Si no se define, usa `./working-dir`.

Opcional:

- PostgreSQL: `MULTIAGENT_PG_*`, si no se usa Docker.
- Redis: `REDIS_URL`, si no se usa Docker.
- Twilio: `TWILIO_*`, solo si se quiere WhatsApp.
- Datos personales: `USER_SYSTEM`, `USER_PHONE`, `USER_EMAIL`. No son necesarios para arrancar.

No se debe commitear `.env`, claves, dumps de base de datos, `working-dir/`, screenshots privados ni memoria personal.

## Arquitectura

```text
FastAPI / index.html
        |
        v
AgentRunner
        |
        +-- ExecutorAgent
        +-- DeviceManagerAgent
        +-- WebSearchAgent
        +-- CronosAgent
        |
        +-- tools/local_tools.py schemas
        +-- tools/ticket_dispatcher.py implementations
        |
        +-- PostgreSQL: conversaciones + chunks + embeddings
        +-- Redis: cache de contexto + rate limit Twilio
        +-- OpenAI: Responses API + embeddings
```

## Funcionalidad Principal

- Orquestacion multiagente con agentes configurados en `agents/agent_config.json`.
- Herramientas locales para leer, escribir, editar y buscar archivos.
- Hash `md5` de archivos devuelto por `read_file`, `write_file`, `edit_file` y `file_hash`.
- Ejecucion de comandos con bloqueos basicos de seguridad.
- Sandbox Python limitado para calculos y logica pura.
- Lectura de TXT, Markdown, JSON, CSV, PDF, DOCX, XLSX y PPTX.
- Navegacion web con Playwright.
- Webhook Twilio/WhatsApp con validacion de firma, allowlist, rate limit e idempotencia.
- Workflows reutilizables para procesos recurrentes o habilidades operativas.
- Agentes cron/autonomos capaces de ejecutar tareas periodicas con estado y trazabilidad.
- Memoria conversacional persistente:
  - chunks semanticos con `text-embedding-3-large`
  - almacenamiento en PostgreSQL
  - busqueda vectorial con pgvector/HNSW cuando esta disponible
  - busqueda textual PostgreSQL FTS
  - modo hibrido por Reciprocal Rank Fusion

## Variables De Entorno

Minimas:

```env
OPENAI_API_KEY=sk-...

MULTIAGENT_PG_HOST=localhost
MULTIAGENT_PG_PORT=5432
MULTIAGENT_PG_DB=multiagente
MULTIAGENT_PG_USER=admin
MULTIAGENT_PG_PASSWORD=change-me
MULTIAGENT_PG_SCHEMA=multiagente

REDIS_URL=redis://localhost:6379
WORKING_PATH=./working-dir
```

Opcionales utiles:

```env
# API HTTP: si se define, /chat y endpoints de conversaciones requieren X-API-Key
CHAT_API_KEY=change-me

# Modelos/memoria
MEMORY_RETRIEVAL_LIMIT=5
MEMORY_MIN_SIMILARITY=0.55
MEMORY_RETRIEVAL_MODE=vector   # vector | keyword | hybrid
MEMORY_VECTOR_WEIGHT=0.7
MEMORY_KEYWORD_WEIGHT=0.3

# Twilio/WhatsApp
PUBLIC_BASE_URL=https://tu-dominio-o-ngrok
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_VALIDATE_SIGNATURE=true
TWILIO_ALLOWED_FROM=whatsapp:+34123456789
TWILIO_RATE_LIMIT_PER_MINUTE=10
TWILIO_RATE_LIMIT_PER_DAY=100
TWILIO_GLOBAL_RATE_LIMIT_PER_MINUTE=30
TWILIO_MAX_INBOUND_WORDS=1000
TWILIO_MAX_REPLY_WORDS=250

# Datos personales inyectados en prompts
USER_SYSTEM=ubuntu-wsl2
USER_PHONE=+34...
USER_EMAIL=...
USER_PREFERENCES_PATH=./working-dir/memory/user_preferences.txt
CRONS_PATH=./working-dir/crons
WORKFLOW_PATH=./working-dir/workflows
```

## Arranque Con Docker

El camino mas reproducible es Docker Compose. Levanta:

- `app`: FastAPI
- `db`: PostgreSQL con pgvector
- `redis`: Redis

1. Exporta al menos tu clave de OpenAI:

```bash
export OPENAI_API_KEY="sk-..."
```

2. Levanta servicios:

```bash
docker compose up --build
```

3. Abre:

```text
http://localhost:8000
```

La app usa por defecto dentro de Docker:

```env
MULTIAGENT_PG_HOST=db
MULTIAGENT_PG_DB=multiagente
MULTIAGENT_PG_USER=admin
MULTIAGENT_PG_PASSWORD=multi-claw-dev
REDIS_URL=redis://redis:6379
WORKING_PATH=/tmp/planner
```

Para parar:

```bash
docker compose down
```

Para borrar datos tambien:

```bash
docker compose down -v
```

## Arranque Local Sin Docker

Requisitos:

- Python 3.11 recomendado.
- PostgreSQL accesible.
- Redis accesible si quieres cache/rate limit persistente.
- `OPENAI_API_KEY`.

Instala dependencias minimas:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Arranca:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Si arrancas desde otro directorio, las rutas relativas configuradas en `.env` se resuelven respecto a la raiz del proyecto cuando pasan por `app_paths.py`.

## API

### Chat

```http
POST /chat
Content-Type: application/json
X-API-Key: <solo si CHAT_API_KEY esta definido>
```

```json
{
  "session_id": "demo",
  "username": "usuario",
  "message": "Hola",
  "conversation_type": null
}
```

`POST /chat` tambien acepta `images` con `url`, `path`, `data_url`, `file_id` o `base64`.

### Conversaciones

- `GET /conversations`
- `GET /conversations/{session_id}`
- `DELETE /session/{session_id}`
- `DELETE /conversations/{session_id}`

### Twilio

- `POST /twilio/webhook`

El webhook valida firma de Twilio si `TWILIO_VALIDATE_SIGNATURE=true`, limita remitentes con `TWILIO_ALLOWED_FROM`, aplica rate limits y divide respuestas largas en mensajes de maximo 250 palabras por defecto.

## Herramientas De Agente

Las herramientas se registran en dos sitios:

- Esquema: `tools/local_tools.py`
- Implementacion: `tools/ticket_dispatcher.py`

Si una tool se quiere exponer a un agente, se anade tambien en `agents/agent_config.json`.

## Memoria

`tools/memoryTools/RAG_memory.py` hace:

1. Divide texto conversacional en chunks semanticos.
2. Genera embeddings con OpenAI.
3. Guarda chunks en `multiagente.conversation_chunks`.
4. Recupera memoria por:
   - vector: `embedding <=> query_embedding`
   - keyword: PostgreSQL FTS sobre `chunck`
   - hybrid: fusion de rankings

La tabla usa columna `embedding halfvec(3072)` si pgvector esta disponible. Si no lo esta, se usa `JSONB` y la busqueda vectorial devuelve vacio.

El workflow versionado `working-dir/workflows/memory_retrieval_tutorial` documenta como consultar esa memoria con buena relacion señal/tokens:

- clasificar primero la intencion de la consulta
- usar `conversation_chunks` como indice semantico/operativo
- reducir el espacio de busqueda antes de abrir contexto grande
- priorizar 1 resultado por sesion en primeras pasadas
- combinar filtros textuales, prefijos, FTS/BM25-like, vectorial e hibrida
- abrir `conversations.context_jsonb` solo para literal exacto, atribucion o cronologia fiel

## Tests

```bash
python -m unittest
```

Los tests actuales son unitarios y no requieren OpenAI real, Postgres ni Redis si el entorno esta razonablemente aislado.
Por como estan inicializados algunos clientes globales, hoy si conviene tener `OPENAI_API_KEY`
definida aunque sea con un valor de desarrollo; los tests no hacen llamadas reales a OpenAI.

## Seguridad Antes De Compartir O Exponer

Este sistema debe tratarse como una herramienta local con mucho poder. Antes de pasarlo de uso personal a uso compartido o publico, revisar:

- Definir siempre `CHAT_API_KEY` si la API es accesible desde otra maquina.
- Mantener `TWILIO_VALIDATE_SIGNATURE=true` si se usa Twilio.
- Configurar `TWILIO_ALLOWED_FROM` para aceptar solo numeros conocidos.
- Cambiar contrasenas de desarrollo de Docker antes de usarlo fuera de local.
- No publicar puertos de PostgreSQL y Redis si no son necesarios fuera del host.
- Restringir CORS en `main.py`; `allow_origins=["*"]` es comodo para desarrollo, no para produccion.
- Revisar agentes con herramientas de archivos/comandos antes de dar acceso a usuarios no confiables.
- Mantener `ALLOWED_WRITE_ROOTS` limitado a carpetas de trabajo controladas si se despliega para terceros.
- Rotar cualquier clave que haya estado en `.env`, logs, capturas o historial de terminal.
- Revisar `working-dir/` antes de compartir la maquina o copiar volumenes: puede contener memoria, preferencias y archivos generados.

## Mejoras Recomendadas

Prioridad alta:

- Crear `.env.example` sin secretos con las variables minimas y recomendadas.
- Separar modo `dev` y modo `prod`: CORS, credenciales, puertos expuestos, logs y herramientas habilitadas.
- Hacer que PostgreSQL y Redis no expongan puertos en Docker Compose salvo que se active explicitamente.
- Anadir autenticacion mas fuerte si se comparte con mas usuarios: usuarios reales, sesiones, permisos por herramienta.
- Auditar las herramientas de comando y escritura para que tengan allowlists mas estrictas por entorno.
- Formalizar benchmarks de memoria: recall, precision, coste por query y comportamiento con multiples temporalidades.

Prioridad media:

- Crear healthcheck HTTP documentado para saber si API, DB, Redis y OpenAI estan configurados.
- Anadir tests de arranque de configuracion para validar rutas, `.env` y defaults.
- Documentar como hacer backup/restauracion de PostgreSQL y del volumen `planner-workspace`.
- Convertir `requirements.docker.txt` y `requirements.txt` en una unica fuente o explicar claramente cuando usar cada uno.
- Anadir CI con `python -m unittest` y una build de Docker.

Prioridad baja:

- Mejorar nombres y ortografia de prompts internos (`worflows`, `incovar`, etc.) sin cambiar comportamiento.
- Crear una pequena pagina de diagnostico en la UI para mostrar API URL, auth activa y estado de servicios.
- Documentar ejemplos de uso de `/chat`, Twilio y memoria con datos ficticios.

## Limitaciones Conocidas

- Aunque las rutas base son portables, la memoria o preferencias existentes pueden contener rutas personales si vienen de un entorno anterior.
- Redis aun esta acoplado al runner principal; conviene hacer fallback in-memory para todo el flujo, no solo Twilio.
- La API general solo queda protegida si configuras `CHAT_API_KEY`.
- CORS esta abierto (`*`) en `main.py`; bien para desarrollo, no ideal para produccion.
- Twilio responde en el mismo request. Si el agente tarda demasiado, conviene pasar a procesamiento background y responder por Twilio REST API.

## Estructura

```text
.
├── main.py
├── app_paths.py
├── index.html
├── agents/
│   ├── agent_builder.py
│   ├── agent_config.json
│   └── agent_prompts.py
├── runner/
│   └── agent_runner.py
├── tools/
│   ├── local_tools.py
│   ├── ticket_dispatcher.py
│   └── memoryTools/
│       ├── RAG_memory.py
│       └── semantic_splitter.py
├── data/
│   ├── conversation_store.py
│   ├── redis_manager.py
│   └── schemas.py
├── integrations/
│   └── twilio/
│       └── router.py
├── tests/
├── working-dir/
│   └── workflows/
│       └── memory_retrieval_tutorial/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements.docker.txt
```
