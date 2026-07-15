-- Playbook SQL reutilizable para consultas óptimas de memoria
-- Esquema: multiagente.conversations, multiagente.conversation_chunks
-- Nota: la columna correcta es `chunck`.

-- =====================================================
-- 1) Sesiones recientes
-- =====================================================
SELECT session_id, conversation_type, created_at, updated_at
FROM multiagente.conversations
ORDER BY updated_at DESC
LIMIT 20;

-- =====================================================
-- 2) Mensajes del usuario desde context_jsonb
-- Ajusta la ruta JSON si tu despliegue usa otra clave canónica.
-- =====================================================
SELECT
  c.session_id,
  msg.ord AS message_order,
  msg.elem ->> 'role' AS role,
  msg.elem ->> 'content' AS content
FROM multiagente.conversations c,
LATERAL jsonb_array_elements(COALESCE(c.context_jsonb -> 'messages', '[]'::jsonb)) WITH ORDINALITY AS msg(elem, ord)
WHERE msg.elem ->> 'role' = 'user'
ORDER BY c.updated_at DESC, msg.ord ASC
LIMIT 50;

-- =====================================================
-- 3) Trazas de agentes por prefijo anclado
-- =====================================================
SELECT session_id, message_order, LEFT(chunck, 300) AS sample
FROM multiagente.conversation_chunks
WHERE chunck ~ '^\[(ExecutorAgent assistant|ExecutorAgent user|DeviceManagerAgent assistant|DeviceManagerAgent user|DeviceManagerAgent function_output|DeviceManagerAgent function_call )'
ORDER BY created_at DESC
LIMIT 30;

-- =====================================================
-- 4) Archivos / resultados / artefactos
-- =====================================================
WITH focus AS (
  SELECT session_id, message_order, chunck
  FROM multiagente.conversation_chunks
  WHERE chunck ~ '^\[(ExecutorAgent assistant|DeviceManagerAgent assistant|DeviceManagerAgent function_output):'
    AND length(chunck) BETWEEN 120 AND 900
    AND (
      chunck ~ '/home/|/mnt/[cd]/'
      OR chunck ~* '(path|ruta|archivo|archivos|bytes_written|created|modified|resultado|artifact|artifacts)'
    )
)
SELECT session_id, message_order, LEFT(chunck, 260) AS sample
FROM focus
ORDER BY message_order DESC
LIMIT 20;

-- =====================================================
-- 5) Estado / progreso / siguiente paso / bloqueo
-- =====================================================
WITH focus AS (
  SELECT session_id, message_order, chunck
  FROM multiagente.conversation_chunks
  WHERE chunck ~ '^\[(ExecutorAgent assistant|DeviceManagerAgent assistant|DeviceManagerAgent function_output):'
    AND length(chunck) BETWEEN 120 AND 900
    AND chunck ~* '(status|estado|progress|progreso|plan|siguiente|bloqueo|current_focus|pendiente|next)'
)
SELECT session_id, message_order, LEFT(chunck, 260) AS sample
FROM focus
ORDER BY message_order DESC
LIMIT 20;

-- =====================================================
-- 6) Vectorial híbrida sobre subconjunto filtrado
-- Requiere $EMBEDDING$ y embed_text al usar memory_query.
-- =====================================================
WITH focus AS (
  SELECT session_id, message_order, chunck, embedding
  FROM multiagente.conversation_chunks
  WHERE chunck ~ '^\[(ExecutorAgent assistant|DeviceManagerAgent assistant|DeviceManagerAgent function_output):'
    AND length(chunck) BETWEEN 120 AND 900
    AND chunck ~* '(status|estado|resumen|resultado|ruta|path|archivo|archivos|progress|progreso|plan|bloqueo|bytes_written)'
)
SELECT session_id, message_order, LEFT(chunck, 220) AS sample,
       embedding <=> $EMBEDDING$::halfvec(3072) AS dist
FROM focus
ORDER BY dist ASC
LIMIT 10;

-- embed_text sugerido:
-- estado actual, resumen breve, archivos creados o modificados, resultado final, siguiente paso

-- =====================================================
-- 7) FTS/BM25-like sobre subconjunto filtrado
-- =====================================================
WITH focus AS (
  SELECT session_id, message_order, chunck
  FROM multiagente.conversation_chunks
  WHERE chunck ~ '^\[(ExecutorAgent assistant|DeviceManagerAgent assistant|DeviceManagerAgent function_output):'
    AND length(chunck) BETWEEN 120 AND 900
    AND chunck ~* '(status|estado|resumen|resultado|ruta|path|archivo|archivos|progress|progreso|plan|bloqueo|bytes_written)'
), q AS (
  SELECT websearch_to_tsquery('simple', 'estado resultado archivo progreso') AS query
)
SELECT f.session_id, f.message_order, LEFT(f.chunck, 220) AS sample,
       ts_rank_cd(to_tsvector('simple', COALESCE(f.chunck, '')), q.query) AS score
FROM focus f, q
WHERE to_tsvector('simple', COALESCE(f.chunck, '')) @@ q.query
ORDER BY score DESC, f.message_order DESC
LIMIT 10;

-- Ajusta la cadena de websearch_to_tsquery según el objetivo literal.

-- =====================================================
-- 8) Conteo de eventos reales deduplicados por clave estable
-- Patrón seguro:
--   1) candidate_sessions: localizar sesiones probables con filtros baratos.
--   2) candidate_chunks: reunir evidencias candidatas, sin contar todavía.
--   3) verified_events: extraer SOLO claves explícitas y específicas del dominio.
-- Si no existe clave estable, NO hagas fallback heurístico para contar;
-- abre context_jsonb/artefactos/logs puntuales y reporta confirmados vs ambiguos.
--
-- Ajusta SIEMPRE estos placeholders antes de ejecutar:
--   <DOMAIN_TERMS>       términos del dominio: 'deploy|invoice|gmail|print|...'
--   <EVENT_KEY_REGEX>    regex específico con 1 grupo capturando la clave estable.
-- Ejemplos orientativos, no universales:
--   '(?:job_id|job id)[:= ]+([0-9]+)'
--   '(?:message_id|gmail_message_id|threadId)[:= \"'']+([A-Za-z0-9_-]+)'
--   '(?:transaction_id|task_id|run_id)[:= \"'']+([A-Za-z0-9_-]+)'
-- Evita regex genéricas tipo '\bid\b' sin prefijo de dominio: suelen ser ruido.
-- =====================================================
WITH candidate_sessions AS (
  SELECT DISTINCT session_id
  FROM multiagente.conversation_chunks
  WHERE chunck ~* '<DOMAIN_TERMS>'
  ORDER BY session_id
  LIMIT 50
), candidate_chunks AS (
  SELECT cc.session_id, cc.message_order, cc.chunck
  FROM multiagente.conversation_chunks cc
  JOIN candidate_sessions cs USING (session_id)
  WHERE cc.chunck ~* '<DOMAIN_TERMS>'
    AND cc.chunck ~* '<EVENT_KEY_REGEX>'
), verified_events AS (
  SELECT
    session_id,
    message_order,
    (regexp_match(chunck, '<EVENT_KEY_REGEX>', 'i'))[1] AS event_key,
    LEFT(chunck, 220) AS sample
  FROM candidate_chunks
  WHERE regexp_match(chunck, '<EVENT_KEY_REGEX>', 'i') IS NOT NULL
)
SELECT COUNT(DISTINCT event_key) AS distinct_verified_events,
       COUNT(*) AS evidence_rows,
       COUNT(DISTINCT session_id) AS sessions_touched
FROM verified_events;

-- Auditoría recomendada antes de responder:
-- SELECT event_key, COUNT(*) AS n, array_agg(DISTINCT session_id) AS sessions, max(sample) AS sample
-- FROM verified_events GROUP BY event_key ORDER BY n DESC LIMIT 50;
--
-- Si verified_events queda vacío o incompleto, usa candidate_sessions/candidate_chunks
-- solo como shortlist y abre fuentes puntuales. No sustituyas una clave ausente por md5(chunk).

-- =====================================================
-- 9) Conteo directo de escrituras por herramienta + ruta objetivo
-- Objetivo: contar cambios reales en archivos/workflows sin abrir context_jsonb grande.
-- Ajusta SIEMPRE:
--   <CURRENT_SESSION_ID>      sesión actual a excluir si se pregunta por historial pasado.
--   <TARGET_PATH_OR_SLUG>     ruta o slug objetivo, ej. personal_data_vault.
--   <SINCE_TIMESTAMP>         fecha inferior opcional; usa '1970-01-01' si no aplica.
--
-- Señales incluidas:
--   - function_call write_file / edit_file
--   - bytes_written / replacements / file_hash
--   - CLI específico tipo vault.py set cuando el workflow lo permita
-- No cuenta menciones genéricas: exige señal de herramienta/evento + ruta/slug objetivo.
-- Si queda duda entre plan y ejecución, abre context_jsonb solo para esas sesiones.
-- =====================================================
WITH candidate_chunks AS (
  SELECT
    session_id,
    message_order,
    created_at,
    chunck
  FROM multiagente.conversation_chunks
  WHERE session_id <> '<CURRENT_SESSION_ID>'
    AND created_at >= TIMESTAMPTZ '<SINCE_TIMESTAMP>'
    AND chunck ~* '<TARGET_PATH_OR_SLUG>'
    AND (
      chunck ~* 'function_call[^\n]*(write_file|edit_file)'
      OR chunck ~* 'bytes_written|replacements|file_hash'
      OR chunck ~* 'vault\.py[[:space:]]+set'
    )
    AND NOT (chunck ~* '(plan|planned|snippet|summary|resumen|mención|mencion|deber[ií]a|voy a|would)'
             AND chunck !~* 'function_output|replacements|bytes_written|file_hash')
), classified AS (
  SELECT
    session_id,
    message_order,
    created_at,
    CASE
      WHEN chunck ~* 'function_call[^\n]*write_file|bytes_written' THEN 'write_file'
      WHEN chunck ~* 'function_call[^\n]*edit_file|replacements' THEN 'edit_file'
      WHEN chunck ~* 'vault\.py[[:space:]]+set' THEN 'workflow_cli_set'
      WHEN chunck ~* 'file_hash' THEN 'hash_evidence'
      ELSE 'other_write_signal'
    END AS event_kind,
    LEFT(chunck, 260) AS sample
  FROM candidate_chunks
)
SELECT
  event_kind,
  COUNT(*) AS evidence_rows,
  COUNT(DISTINCT session_id) AS sessions_touched,
  MIN(created_at) AS first_seen,
  MAX(created_at) AS last_seen
FROM classified
GROUP BY event_kind
ORDER BY last_seen DESC, evidence_rows DESC;

-- Auditoría puntual:
-- SELECT session_id, message_order, created_at, event_kind, sample
-- FROM classified
-- ORDER BY created_at DESC, message_order DESC
-- LIMIT 80;

-- =====================================================
-- 10) Fingerprint agregado por sesión
-- =====================================================
WITH typed AS (
  SELECT
    cc.session_id,
    CASE
      WHEN cc.chunck ~ '^\[ExecutorAgent user:' THEN 'exec_user'
      WHEN cc.chunck ~ '^\[ExecutorAgent assistant:' THEN 'exec_assistant'
      WHEN cc.chunck ~ '^\[DeviceManagerAgent user:' THEN 'dev_user'
      WHEN cc.chunck ~ '^\[DeviceManagerAgent assistant:' THEN 'dev_assistant'
      WHEN cc.chunck ~ '^\[DeviceManagerAgent function_output:' THEN 'dev_output'
      WHEN cc.chunck ~ '^\[DeviceManagerAgent function_call ' THEN 'dev_call'
      ELSE 'other'
    END AS t,
    cc.chunck
  FROM multiagente.conversation_chunks cc
), sig AS (
  SELECT
    session_id,
    COUNT(*) AS n_chunks,
    COUNT(*) FILTER (WHERE chunck ~* '(bytes_written|replacements|archivo_creado|archivos_modificados|archivos_escritos|created|modificado)') AS n_writeish,
    COUNT(*) FILTER (WHERE chunck ~* '(read_file|mime|kind|truncated|content|lectura|le[ií]do)') AS n_readish,
    COUNT(*) FILTER (WHERE chunck ~* '(status|estado|resumen|resultado|acciones_realizadas|conclusion|bloqueo|progress|progreso)') AS n_summaryish
  FROM typed
  GROUP BY session_id
)
SELECT session_id, n_chunks, n_writeish, n_readish, n_summaryish,
       CASE
         WHEN n_writeish >= 15 THEN 'write-heavy'
         WHEN n_readish >= 20 THEN 'read-heavy'
         WHEN n_summaryish >= 25 THEN 'summary-heavy'
         ELSE 'mixed'
       END AS inferred_kind
FROM sig
ORDER BY n_writeish DESC, n_summaryish DESC, n_chunks DESC
LIMIT 30;
