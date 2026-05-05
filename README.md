# Multi-Claw

Multi-Claw es un backend experimental de orquestacion multiagente con FastAPI, OpenAI Responses API, memoria persistente en PostgreSQL y cache de contexto en Redis.

El proyecto esta orientado a uso personal/local, pero ya incluye piezas para funcionar como servicio: API HTTP, UI HTML simple, webhook de Twilio/WhatsApp, herramientas locales de archivos/comandos, memoria semantica por embeddings y busqueda textual.

## Estado Real

Este repositorio no es todavia un producto empaquetado. Estas son las realidades importantes:

- Requiere `OPENAI_API_KEY` para ejecutar agentes y generar embeddings.
- Requiere PostgreSQL para persistencia de conversaciones.
- Redis se usa como cache de contexto y para rate limits/idempotencia de Twilio cuando esta disponible.
- La busqueda vectorial usa `pgvector` si la extension esta disponible; si no, los embeddings se guardan como JSONB y no hay busqueda vectorial.
- No se usa Qdrant en runtime.
- Twilio es opcional, pero el router esta incluido en la app.
- El sandbox de `run_python` usa APIs Linux (`resource`, `prctl`), asi que esta pensado para Linux/WSL/Docker.
- Algunas rutas y prompts siguen siendo personales y deberian moverse a configuracion antes de un despliegue multiusuario.

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
WORKING_PATH=/tmp/planner
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
USER_PREFERENCES_PATH=/tmp/planner/user_preferences.txt
CRONS_PATH=/tmp/planner/crons
WORKFLOW_PATH=/tmp/planner/workflows
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
pip install -r requirements.docker.txt
playwright install chromium
```

Arranca:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

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

## Tests

```bash
python -m unittest
```

Los tests actuales son unitarios y no requieren OpenAI real, Postgres ni Redis si el entorno esta razonablemente aislado.
Por como estan inicializados algunos clientes globales, hoy si conviene tener `OPENAI_API_KEY`
definida aunque sea con un valor de desarrollo; los tests no hacen llamadas reales a OpenAI.

## Limitaciones Conocidas

- `requirements.txt` historico viene de un entorno local grande. Para Docker/local limpio usa `requirements.docker.txt`.
- Hay prompts con preferencias/rutas personales que conviene parametrizar antes de compartir el sistema.
- Redis aun esta acoplado al runner principal; conviene hacer fallback in-memory para todo el flujo, no solo Twilio.
- La API general solo queda protegida si configuras `CHAT_API_KEY`.
- CORS esta abierto (`*`) en `main.py`; bien para desarrollo, no ideal para produccion.
- Twilio responde en el mismo request. Si el agente tarda demasiado, conviene pasar a procesamiento background y responder por Twilio REST API.

## Estructura

```text
.
├── main.py
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
├── Dockerfile
├── docker-compose.yml
└── requirements.docker.txt
```
