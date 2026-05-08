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
-- 8) Fingerprint agregado por sesión
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
