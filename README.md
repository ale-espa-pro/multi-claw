<div align="center">

# 🦾 Multi-Claw

**Sistema multi-agente autónomo con memoria semántica persistente**

[![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![OpenAI](https://img.shields.io/badge/OpenAI-GPT--5-412991?style=flat-square&logo=openai&logoColor=white)](https://openai.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Async-336791?style=flat-square&logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-Sessions-DC382D?style=flat-square&logo=redis&logoColor=white)](https://redis.io)
[![License](https://img.shields.io/badge/License-Personal-orange?style=flat-square)](#)

</div>

---

## ¿Qué es Multi-Claw?

Multi-Claw es un framework de orquestación de agentes IA diseñado para ejecutar tareas complejas de forma autónoma. Varios agentes especializados colaboran entre sí, comparten contexto y mantienen memoria persistente entre sesiones usando embeddings semánticos y PostgreSQL.

> Fork personal de openclaw con arquitectura de memoria mejorada, soporte para WhatsApp/Twilio, ejecución paralela de herramientas y más de 50 herramientas nativas.

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────┐
│                        Web UI / API                         │
│                    FastAPI  ·  WebSocket                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                     ExecutorAgent  (main)                   │
│         Orquesta, delega y coordina sub-agentes             │
└───┬──────────┬──────────┬──────────┬──────────┬────────────┘
    │          │          │          │          │
    ▼          ▼          ▼          ▼          ▼
 WebSearch  DeviceMgr  Cronos    MCPMgr    Planner
  Agent      Agent     Agent     Agent     Agent
  (web)    (sistema)  (memoria) (MCP)    (planning)
    │          │          │          │          │
    └──────────┴──────────┴──────────┴──────────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
    PostgreSQL           Redis           Qdrant
  (contexto+chunks)   (sesiones)    (RAG opcional)
         │
         ▼
  Embeddings OpenAI
  text-embedding-3-large
  halfvec(3072) dims
```

---

## Agentes

| Agente | Rol | Herramientas clave |
|---|---|---|
| **ExecutorAgent** | Orquestador principal | Todos los sub-agentes |
| **WebSearchAgent** | Búsqueda y navegación web | `web_fetch`, `playwright_navigate` |
| **DeviceManagerAgent** | Control del sistema | `run_command`, `read_file`, `write_file`, `run_python`|
| **PlannerAgent** | Planificación de tareas | Descomposición de objetivos (DESACTIVADO EN MODELOS RAZONADORES/INNECESARIO)

---

## Características principales

### 🧠 Memoria semántica persistente
- Conversaciones divididas en chunks semánticos con embeddings OpenAI
- Búsqueda por similitud vectorial sobre el historial completo
- Búsqueda textual FTS/BM25-like opcional y modo híbrido con fusión de rankings
- Sincronización en background sin bloquear la respuesta

### ⚡ Ejecución paralela de herramientas
- Los agentes pueden ejecutar múltiples herramientas simultáneamente
- Arquitectura async/await de extremo a extremo
- Bloqueo de sesión para escenarios multi-usuario

### 🗄️ Persistencia dual
- **PostgreSQL**: snapshots completos del contexto en JSONB + embeddings halfvec
- **Redis**: caché de sesión para acceso rápido

### 📲 Integración WhatsApp / Twilio
- Webhook listo para recibir mensajes de WhatsApp
- Respuestas automáticas vía Twilio API

### 📊 Token tracking con caché de prompts
- Monitoreo en tiempo real: tokens de entrada, salida y cacheados
- Cálculo automático del cache hit rate
- Reportes de uso por sesión

### 🔒 Seguridad incorporada
- Comandos bloqueados configurables
- Ejecución Python en sandbox (En progreso)
- SQL de solo lectura para agentes
- Validación estricta con Pydantic

---

## Tipos de conversación

| Tipo | Descripción | Persistencia |
|---|---|---|
| `normal` | Conversación estándar con historial completo | ✅ PostgreSQL + Redis |
| `cron` | Tarea programada automática | ✅ PostgreSQL |
| `workflow` | Procedimiento multi-paso reutilizable | ✅ PostgreSQL |

---

## Stack tecnológico

```
Backend    FastAPI · Python 3.8+ · asyncio
IA         OpenAI GPT-5-5.5 · text-embedding-3-large
DB         PostgreSQL (psycopg async) · Redis
Vector     pgvector halfvec(3072) · Qdrant (opcional)
Frontend   Vanilla JS · HTML/CSS dark theme
Mensajería Twilio · WhatsApp Business API
Browser    Playwright (automatización web)
```

---

## Configuración rápida

### 1. Variables de entorno (`.env`)

```env
# OpenAI
OPENAI_API_KEY=sk-...

# PostgreSQL
MULTIAGENT_PG_HOST=localhost
MULTIAGENT_PG_PORT=5432
MULTIAGENT_PG_DB=multiagente
MULTIAGENT_PG_USER=user
MULTIAGENT_PG_PASSWORD=password

# Redis
REDIS_URL=redis://localhost:6379

# Twilio (opcional)
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...

# Usuario
USER_NAME=nombre
USER_PHONE=+34...
USER_EMAIL=email@dominio.com
```

### 2. Instalación

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Iniciar servidor

```bash
uvicorn main:app --reload --port 8000
```

Abre `index.html` en el navegador o accede a `http://localhost:8000`.

---

## Endpoints API

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/chat` | Enviar mensaje a un agente |
| `GET` | `/conversations` | Listar todas las conversaciones |
| `GET` | `/conversations/{id}` | Detalle de una conversación |
| `DELETE` | `/session/{id}` | Eliminar sesión |
| `POST` | `/twilio/webhook` | Webhook de WhatsApp/Twilio |

---

## Ventajas frente a soluciones genéricas

| Característica | Multi-Claw | Frameworks genéricos |
|---|---|---|
| Memoria semántica entre sesiones | ✅ Nativo | ⚠️ Plugin adicional |
| Agentes especializados con roles | ✅ Configurables | ⚠️ Manual |
| Ejecución de herramientas en paralelo | ✅ Nativo async | ⚠️ Variable |
| Integración WhatsApp lista | ✅ Twilio integrado | ❌ No incluida |
| Token tracking con caché | ✅ Automático | ❌ No incluido |
| UI web incluida | ✅ Dark theme | ❌ No incluida |
| Control de concurrencia por sesión | ✅ Session locking | ❌ No incluido |

---

## Estructura del proyecto

```
multi-claw/
├── main.py                    # Entrada FastAPI
├── index.html                 # UI web
├── agents/
│   ├── agent_builder.py       # Carga y configura agentes
│   ├── agent_prompts.py       # Prompts de sistema
│   └── agent_config.json      # Definición de agentes y herramientas
├── runner/
│   └── agent_runner.py        # Motor de ejecución y orquestación
├── tools/
│   ├── local_tools.py         # Schemas de herramientas (50+)
│   ├── ticket_dispatcher.py   # Lógica de ejecución
│   └── memoryTools/
│       ├── RAG_memory.py      # Sistema RAG semántico
│       └── semantic_splitter.py
├── data/
│   ├── conversation_store.py  # PostgreSQL async
│   ├── redis_manager.py       # Gestión de sesiones Redis
│   └── schemas.py             # Modelos Pydantic
├── pricing/
│   └── token_tracker.py       # Monitoreo de tokens
└── integrations/
    └── twilio/
        └── router.py          # Webhook WhatsApp
```

---

<div align="center">

Construido sobre OpenClaw · Versión personal con memoria mejorada

</div>
=======
# multi-claw
Versión personal openclaw multiagente memoria mejorada

## Multimodal input

`POST /chat` accepts optional `images` in addition to `message`. Each image can be provided as `url`, `path`, `data_url`, `file_id`, or raw `base64` with optional `mime_type` and `detail` (`low`, `high`, `auto`). Images are passed to the main Responses API call as `input_image` parts so the main agent can interpret them directly.

`playwright_navigate` returns compact snapshots by default. Screenshots are saved to disk and return metadata/path unless `screenshot_mode` explicitly requests base64.
>>>>>>> 52dded7 (test added and minor changes)
