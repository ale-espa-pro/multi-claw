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
