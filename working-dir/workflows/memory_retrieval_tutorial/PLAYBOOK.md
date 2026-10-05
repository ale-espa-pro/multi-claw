# Playbook consolidado de consultas de memoria

## Propósito

Este documento consolida la guía extensa del workflow `memory_retrieval_tutorial` para consultar la memoria multiagente con **máxima señal por token**. La idea central es siempre la misma: **primero reducir el espacio de búsqueda con estructura, luego aplicar texto o semántica, y abrir `context_jsonb` solo cuando haga falta precisión literal**.

Compatibilidad: el nombre histórico `memory_query_playbook` se mantiene como alias retrocompatible cuando exista.

## Modelo práctico de la memoria

### `multiagente.conversations`
Úsala cuando necesites:
- literal exacto del usuario
- cronología fiel
- reconstruir el contexto real de una sesión
- distinguir roles con fiabilidad
- verificar quién dijo qué

Campo clave:
- `context_jsonb`

### `multiagente.conversation_chunks`
Úsala cuando necesites:
- búsqueda semántica u operativa
- localizar rutas, artefactos, estados, errores, resúmenes y outputs
- hacer retrieval barato y compacto
- enrutar la búsqueda antes de abrir contexto grande

Campo clave:
- `chunck` (sí, con typo)

**Regla importante:** `conversation_chunks` es un **índice semántico/operativo**, no una transcripción fiel 1:1.

## Regla de oro

1. Clasifica la intención de la consulta.
2. Reduce a un subconjunto útil de sesiones o chunks.
3. Si puedes, devuelve **1 resultado por sesión** en la primera pasada.
4. Solo después aplica texto o vectorial.
5. Abre `context_jsonb` únicamente si necesitas literal exacto, atribución o cronología real.

## Taxonomía rápida de consultas

### 1) Dato concreto
Ejemplos:
- teléfono
- fecha
- ruta
- archivo
- nombre de artefacto
- valor puntual

**Estrategia recomendada:** `conversations` + filtros estructurales + apertura puntual de `context_jsonb` si hace falta precisión.

### 2) Resumen general
Ejemplos:
- qué pasó
- qué se hizo
- conclusión
- panorama

**Estrategia recomendada:** `conversation_chunks` con filtros textuales/estructurales y **1 resultado por sesión**.

### 3) Estado / progreso / plan continuo
Ejemplos:
- en qué estado quedó
- siguiente paso
- bloqueo actual
- foco actual

**Estrategia recomendada:** `conversation_chunks` con prefijos y gating de estado; abrir sesión concreta solo al final.

### 4) Archivos / resultados / cambios
Ejemplos:
- qué archivos se crearon
- qué se modificó
- dónde quedó el resultado
- qué artefactos salieron

**Estrategia recomendada:** `conversation_chunks` filtrando rutas, artefactos, resultados, `bytes_written`, `created`, `modified`.

### 5) Semántica abierta
Ejemplos:
- sesiones relacionadas con un tema
- conversaciones parecidas
- ideas o tareas similares

**Estrategia recomendada:** vectorial híbrida sobre subconjunto ya filtrado.

### 6) Literal del usuario
Ejemplos:
- qué dijo exactamente
- cómo formuló la petición
- cronología de mensajes

**Estrategia recomendada:** ir a `context_jsonb` con extracción de mensajes y orden real.

## Pipeline recomendado

### Fase 0 — Clasificación barata de intención
Antes de consultar la DB, identifica si el usuario pide:
- dato concreto
- resumen
- estado/progreso
- archivos/resultados
- semántica abierta
- literal exacto

### Fase 1 — Router barato
Empieza por consultas ligeras:
- sesiones recientes
- filtros por `conversation_type` si ayudan, sin depender solo de eso
- conteos o agregados por sesión
- indicios textuales o estructurales en chunks
- sesiones actualizadas recientemente

**Objetivo:** reducir el espacio de búsqueda antes de gastar tokens.

### Fase 2 — Retrieval compacto
Trabaja sobre subconjuntos densos en información:
- previews cortas
- chunks de longitud moderada
- preferiblemente **1 chunk por sesión** en la primera pasada

### Fase 3 — Refinamiento
Aplica solo dentro del subconjunto correcto:
- regex o `ILIKE`
- FTS/BM25-like rankeado cuando convenga
- búsqueda vectorial
- híbrida texto + vector

### Fase 4 — Fallback de precisión
Abre `context_jsonb` solo si necesitas:
- literal exacto
- cronología real
- atribución por rol
- verificación final
- resolver ambigüedad restante

## Patrones que funcionan bien

### A. Prefijos anclados al inicio del chunk
Suelen funcionar mejor que `ILIKE '%...%'`.

Ejemplos útiles:
- `^\[ExecutorAgent assistant:`
- `^\[ExecutorAgent user:`
- `^\[DeviceManagerAgent user:`
- `^\[DeviceManagerAgent assistant:`
- `^\[DeviceManagerAgent function_output:`
- `^\[DeviceManagerAgent function_call `

### B. Longitud moderada
Filtrar por rangos como:
- `length(chunck) BETWEEN 120 AND 700`
- `length(chunck) BETWEEN 120 AND 900`

Suele mejorar la relación señal/tokens.

### C. Un resultado por sesión
Muy útil para:
- cobertura transversal rápida
- primeras pasadas
- minimizar duplicados por sesión
- decidir dónde profundizar

### D. Gating estructural por familias
Antes de vectorial, filtra por familias útiles.

Familias frecuentes:
- archivos: `path|ruta|archivo|archivos|bytes_written|created|modified|artifact|artifacts`
- estado: `status|estado|progress|progreso|plan|siguiente|bloqueo|current_focus|pendiente|next`
- resumen: `resumen|resultado|conclusion|acciones_realizadas|estado_general`
- errores: `error|failed|exception|timeout|unauthorized|forbidden|blocked`

### E. Vectorial híbrida sobre subconjunto filtrado
La vectorial mejora mucho cuando ya has filtrado por:
- familia
- tipo de chunk
- longitud
- fecha o shortlist de sesiones

### F. Chunks compuestos: extraer la última respuesta de subagente

Un chunk puede agrupar varias trazas: prompt, llamadas de herramienta, outputs y una respuesta final de subagente. Por tanto, una respuesta útil no siempre está anclada al inicio.

Fallback seguro y barato:
1. Prueba primero el prefijo anclado habitual (`^\[... assistant:`).
2. Si devuelve cero resultados o señal incompleta, busca dentro del chunk el marcador `\[(DeviceManagerAgent|WebSearchAgent|PlaywrightSessionAgent) assistant:`.
3. Si hay varios marcadores assistant, extrae **solamente lo posterior al último marcador** y aplica sobre ese payload el gating, scoring, preview y ranking.
4. Si la extracción queda vacía, truncada o ambigua, abre puntualmente `context_jsonb` para esa sesión.

**No puntúes ni devuelvas el chunk compuesto completo tras detectar un marcador interno.** Un marcador interno sin extracción puede mezclar el prompt original, tool traces, recomendaciones intermedias y la respuesta final, causando atribuciones o estados falsos.

### G. “Último resultado” significa último relevante/terminal

No uses ingenuamente `MAX(message_order)` ni el chunk cronológicamente último para inferir el resultado final de una sesión. La recencia debe aplicarse **después** del gating de dominio, tipo de respuesta y terminalidad.

Pipeline recomendado:
1. Filtra al dominio solicitado.
2. Conserva respuestas finales de subagente; para chunks compuestos, normaliza al payload posterior al último marcador assistant.
3. Puntúa por encima las señales terminales: `status`, `confirmada`/`no_confirmada`, `completed`/`failed`/`blocked`, `localizador`, `resultado final` y equivalentes específicos del dominio.
4. Deja descubrimiento, búsqueda, candidatos y recomendaciones por debajo; para una consulta estrictamente terminal, exclúyelos si no contienen evidencia terminal.
5. Solo entre evidencias terminales comparables usa `message_order DESC`, devolviendo **1 resultado por sesión**.
6. Si faltan señales, hay estados incompatibles o el payload no permite atribución fiable, abre solo el `context_jsonb` de las sesiones conflictivas.

`created_at` puede ser idéntico para varios chunks por ingesta batch. `message_order` suele ser mejor desempate dentro de la sesión, pero **no demuestra por sí solo finalidad**.

Aprendizaje genérico: el chunk con mayor `message_order` puede contener una respuesta de búsqueda o recomendación, mientras un chunk anterior conserva el estado operativo terminal. Elegir primero por recencia invierte la prioridad semántica y devuelve el resultado equivocado.

## Troubleshooting / errores frecuentes

- Los guards de operaciones pueden hacer matching léxico sobre keywords incluso dentro de literales SQL. Evita términos DML destructivos en strings de búsqueda; usa sinónimos neutros, filtros `ILIKE` alternativos o búsquedas separadas.

## Patrones que NO conviene usar por defecto

- vectorial sobre todos los `conversation_chunks`
- usar `conversation_chunks` para reconstruir literalmente lo que dijo el usuario
- asumir que `conversation_type` clasifica todo por sí solo
- matching literal débil con cadenas muy cortas (`si`, `ok`, `1`, `2`)
- abrir `context_jsonb` completo como primera opción salvo necesidad real

## Modos operativos por presupuesto de tokens

### Modo ultrabarato
Úsalo para orientación rápida o triage.

Patrón:
- assistant/output solamente
- `length(chunck) BETWEEN 80 AND 260`
- previews de 120 chars
- `LIMIT 5-8`
- si puedes, 1 resultado por sesión

### Modo medio
Úsalo cuando necesitas una respuesta útil y compacta.

Patrón:
- assistant/output
- `length(chunck) BETWEEN 120 AND 700`
- gating estructural por familia
- previews de 180-220 chars
- top 8-10

### Modo detallado controlado
Úsalo cuando hace falta precisión sin abrir todo demasiado pronto.

Patrón:
- fase agregada por sesión
- segunda query sobre 1-3 sesiones candidatas
- solo al final `context_jsonb`

## Heurística rápida de decisión

- Si pide **qué dijo exactamente** → empieza por `context_jsonb`.
- Si pide **qué se hizo / qué cambió** → empieza por `DeviceManagerAgent assistant/function_output` filtrado.
- Si pide **archivos o rutas** → usa gating de archivos.
- Si pide **estado o siguiente paso** → usa gating de estado/progreso.
- Si pide **tema relacionado** → usa vectorial híbrida sobre subconjunto filtrado.
- Si pide **panorama general** → usa **1 resultado por sesión**.
- Si el presupuesto es muy bajo → empieza por agregados o previews ultracortas.

## Cómo contar sin duplicar mal

- Si quieres **cobertura por sesión**, usa `COUNT(DISTINCT session_id)` o una primera pasada con **1 resultado por sesión**.
- Si quieres **eventos reales**, no deduzcas el conteo desde la shortlist por sesión: cuenta los eventos concretos en el subconjunto filtrado.
- La deduplicación por sesión sirve para triage, no para contar todos los outputs de una sesión.

### Conteo directo de escrituras, ediciones y cambios de archivos

- Si el usuario pregunta por **modificaciones, creaciones o cambios de archivos/workflows**, antes de abrir contexto grande filtra directamente `conversation_chunks` por herramientas/eventos específicos: `function_call write_file`, `function_call edit_file`, `bytes_written`, `replacements`, `file_hash`, y por la ruta o slug del workflow/proyecto objetivo.
- Si los cambios pueden hacerse mediante un CLI propio del workflow, incluye también el comando/patrón específico y su workdir/ruta, por ejemplo `vault.py set` dentro del slug `personal_data_vault`, sin valores sensibles.
- Cuando la pregunta sea sobre historial pasado, excluye explícitamente la sesión actual (`session_id <> '<CURRENT_SESSION_ID>'`) para no contar ecos, resúmenes o la investigación en curso.
- Cuenta **llamadas reales de escritura/edición** o hashes cambiados. No cuentes menciones, planes, summaries, snippets derivados ni explicaciones que repitan una acción.
- Abre `context_jsonb` solo de forma puntual si el chunk candidato no permite confirmar que hubo acción real o si necesitas resolver ambigüedad entre plan y ejecución.
- Los aprendizajes de filtrado y conteo van en este workflow (`PLAYBOOK.md`/`QUERY_TEMPLATES.sql`), **no en `save_preference`**, salvo que sean una preferencia estable real del usuario.

## Dónde guardar aprendizajes de retrieval

- Usa `save_preference` solo para **preferencias estables del usuario**: idioma, canales, horarios, criterios persistentes o datos personales permitidos.
- Las mejoras de filtrado, errores frecuentes, casos de uso y heurísticas operativas deben documentarse en este workflow: `PLAYBOOK.md` para reglas, `QUERY_TEMPLATES.sql` para patrones SQL y `README.md` si cambia el uso general.
- No conviertas cada evaluación puntual de retrieval en preferencia. Si un caso enseña una técnica reusable, sintetízala aquí como regla operativa.
- Para **conteos de eventos reales**, separa siempre:
  1. localización de sesiones/chunks candidatos;
  2. verificación en fuente fiable (`context_jsonb` puntual, artefactos estructurados o logs originales);
  3. deduplicación por una clave estable y específica del dominio.
- Evita contar como eventos reales simples menciones, resúmenes o chunks sin evidencia verificable. Si no hay clave estable, reporta confirmados vs ambiguos y no inventes un total automático.
- Distingue evidencia de ejecución `confirmada`, `parcial/intentada` y `ambigua`; una acción fallida puede seguir siendo arriesgada, indicando cuánto llegó a ejecutarse.
- En comparativas de riesgo, ordena la evidencia verificada por impacto, irreversibilidad, alcance o privilegios, exposición de datos o secretos, efecto externo y dificultad de rollback.
- Ejemplos breves de claves estables: job id o documento+timestamp en acciones de sistema; `message_id`/`threadId` en correos; id de transacción, id de tarea o ruta+hash en artefactos. Separa cuando convenga conteo amplio (`scanned/listed/candidato`) y conteo estricto (`selected/executed/downloaded/confirmado`).
- Si un agregado indica elementos no listados (`messages_scanned`, `n_results`, `total_found`, etc.), decláralo y decide explícitamente si representa total fiable, límite inferior o señal incompleta.

## Uso de retrieval automático

Trata el **EXTRA DE MEMORIA AUTOMÁTICA** como una pista:
- evalúalo como `útil`, `neutro` o `ruido`
- no lo asumas como verdad sin verificar
- úsalo para orientar filtros, no para cerrar la respuesta final por sí solo

## Checklist antes de gastar muchos tokens

- ¿Puedo resolverlo con agregados por sesión?
- ¿Necesito de verdad literal exacto del usuario?
- ¿Me conviene 1 resultado por sesión para cubrir más?
- ¿Ya filtré por tipo de chunk y por longitud?
- ¿Si el marcador assistant es interno, extraje solo lo posterior al último marcador?
- ¿Apliqué recencia después del gating de dominio y terminalidad?
- ¿Puedo aplicar gating estructural antes de vectorial?
- ¿Estoy tratando el retrieval automático como pista y no como verdad?
- ¿Estoy evitando depender de nombres concretos de proyectos ya vistos?

## Consultas reutilizables

Las consultas SQL canónicas viven en:
- `QUERY_TEMPLATES.sql`

Ahí están las plantillas para:
- sesiones recientes
- mensajes del usuario desde `context_jsonb`
- trazas de agentes por prefijo anclado
- fallback de marcador assistant interno con extracción del último payload
- último resultado relevante/terminal con 1 resultado por sesión
- archivos/resultados/artefactos
- estado/progreso
- vectorial híbrida sobre subconjunto filtrado
- conteo de eventos reales deduplicados por clave estable
- fingerprint agregado por sesión

## Recomendación operativa final

El patrón más reusable de este workflow es:

1. `conversations` para orientación y contexto estructural.
2. `conversation_chunks` como índice semántico/operativo.
3. prefijos anclados + longitud moderada + gating estructural.
4. **1 resultado por sesión** cuando quieras cobertura barata.
5. vectorial solo dentro del subconjunto correcto.
6. `context_jsonb` solo cuando haga falta precisión literal o cronología.

Si dudas entre dos enfoques, elige el que **reduzca antes el espacio de búsqueda**.

## Retrieval complejo con filtrado Python y acciones externas

1. Usa la DB y `conversation_chunks` solo como router inicial: devuelve IDs y previews cortas, no conversaciones completas.
2. Abre `context_jsonb` únicamente para las sesiones candidatas y filtra/normaliza el resultado en Python.
3. Para acciones externas (por ejemplo, Gmail), exige evidencia terminal de ejecución (`status=sent`, `enviado=true`, `returncode=0` o equivalente). No bastan `from:me`, `SENT`, un borrador, código de ejemplo ni una búsqueda.
4. Deduplica los eventos reales con `message_id`/`threadId` y separa explícitamente confirmados de ambiguos.
5. Si una delegación Python agota iteraciones, reduce el trabajo a las sesiones/IDs ya localizados en vez de repetir una búsqueda global.

## Conteo exacto de tokens

- «Exacto» siempre depende del tokenizer y de su versión; fija y reporta ambos para que el resultado sea reproducible.
- Para `context_jsonb`, cuenta el texto serializado por PostgreSQL con `context_jsonb::text`, usando un tokenizer explícitamente fijado (por ejemplo, `tiktoken` con `cl100k_base`) y `encode_ordinary`.
- Reporta `conversation_chunks.chunck` por separado: puede estar derivado de `context_jsonb` o solaparse con él, así que su suma no equivale a tokens únicos. Como mucho, informa la suma aritmética etiquetada como tal.
- Haz el recuento sobre una instantánea consistente y de solo lectura (por ejemplo, `REPEATABLE READ READ ONLY`), recorriendo los datos sin truncación y sin exponer texto, datos personales ni secretos.

## Nota operativa de auditoría de chunks

`conversation_chunks` no debe tratarse como un espejo 1:1 de `context_jsonb`: el pipeline puede serializar solo deltas y ciertos tipos de elementos, agrupar semánticamente y escribir de forma asíncrona. Antes de inferir pérdida de embeddings, comprueba la cobertura por sesión, las sesiones sin chunks y `embedding IS NOT NULL` junto con su dimensión declarada/observada.

## Aprendizaje 20260820T123655Z — fallback tras FTS cero

Si FTS/BM25-like devuelve cero filas al buscar términos compuestos o identificadores, no se interpreta como ausencia de evidencia. Se hace fallback inmediato a búsqueda vectorial/híbrida sobre el mismo subconjunto ya filtrado, se deduplica a una evidencia por session_id y solo se abre context_jsonb para literalidad exacta o verificación estrictamente necesaria. El cero de FTS es una señal de fallback, no un resultado negativo.

## Nota de despliegue: forma real de context_jsonb y fallback de mensajes

En el despliegue actual, `multiagente.conversations.context_jsonb` suele ser un objeto cuyas claves son agentes (`ExecutorAgent`, `DeviceManagerAgent`, `WebSearchAgent`, `PlaywrightSessionAgent`, etc.) y cuyos valores son arrays de eventos. No existe necesariamente `context_jsonb -> 'messages'`; por eso la plantilla 2 puede devolver cero filas aunque haya mensajes.

Detecta primero `jsonb_typeof(context_jsonb -> 'messages')`:
- si devuelve `array`, usa la ruta canónica de `messages`;
- si devuelve otro tipo o `NULL`, haz fallback a las claves de agente con `jsonb_each` y recorre sus arrays.

Consulta SELECT canónica para el fallback (preview corta y conservadora):

```sql
WITH user_events AS (
  SELECT
    c.session_id,
    c.updated_at,
    agent.agent_key,
    evt.ord AS event_order,
    elem ->> 'role' AS role,
    COALESCE(
      elem #>> '{content,0,text}',
      elem ->> 'content',
      elem ->> 'output',
      elem::text
    ) AS payload
  FROM multiagente.conversations AS c
  CROSS JOIN LATERAL jsonb_each(
    CASE
      WHEN jsonb_typeof(c.context_jsonb) = 'object' THEN c.context_jsonb
      ELSE '{}'::jsonb
    END
  ) AS agent(agent_key, events)
  CROSS JOIN LATERAL jsonb_array_elements(
    CASE
      WHEN jsonb_typeof(agent.events) = 'array' THEN agent.events
      ELSE '[]'::jsonb
    END
  ) WITH ORDINALITY AS evt(elem, ord)
  WHERE jsonb_typeof(c.context_jsonb -> 'messages') IS DISTINCT FROM 'array'
    AND elem ->> 'role' = 'user'
)
SELECT
  CASE
    WHEN length(session_id) > 16 THEN LEFT(session_id, 16) || '...'
    ELSE session_id
  END AS session_id_preview,
  updated_at,
  agent_key,
  event_order,
  role,
  length(payload) AS payload_chars,
  CASE
    WHEN payload IS NULL THEN NULL
    WHEN payload ~* '(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|authorization|cookie|bearer|session[_ -]?id|message[_ -]?id|thread[_ -]?id|user[_ -]?id)'
      THEN '[REDACTED]'
    ELSE LEFT(payload, 240)
  END AS payload_preview
FROM user_events
ORDER BY updated_at DESC, session_id, agent_key, event_order
LIMIT 50;
```

La extracción literal/cronológica debe abrir `context_jsonb` solo para las sesiones candidatas; `conversation_chunks` sigue siendo el router inicial. En previews, trunca los IDs y no muestres valores de secretos (si la sanitización no es fiable, omite el payload).
