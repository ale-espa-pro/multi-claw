description:Workflow reutilizable para consultar la memoria multiagente con máxima señal por token usando routing por intención subconjuntos de chunks búsquedas híbridas y fallback a context_jsonb solo cuando haga falta precisión literal.

# Workflow memory_retrieval_tutorial

> Compatibilidad: alias histórico `memory_query_playbook` -> `working-dir/workflows/memory_retrieval_tutorial`.

## Qué es
Guía corta y canónica para consultar memoria multiagente con buen contexto y bajo coste de tokens.

## Cuándo usarlo
- recuperar estado o progreso
- localizar archivos, rutas o artefactos
- resumir varias sesiones
- buscar literal exacto del usuario
- hacer búsqueda semántica abierta con control de coste

## Quickstart
1. Clasifica la intención: dato concreto, resumen, estado/progreso, archivos/resultados, semántica abierta o literal.
2. Empieza por `multiagente.conversation_chunks` como índice semántico/operativo.
3. Usa subconjuntos compactos, prefijos anclados, gating estructural y longitud moderada.
4. Si hay restricción de tokens, prioriza **1 resultado por sesión** en la primera pasada.
5. Aplica texto (ILIKE/regex/FTS-BM25-like) o vectorial solo dentro del subconjunto correcto.
6. Abre `multiagente.conversations.context_jsonb` solo si necesitas precisión literal, atribución o cronología.

## Archivos del workflow
- `AGENT_PROMPT.md`: versión ultra-corta para ejecución por agentes.
- `PLAYBOOK.md`: guía consolidada larga con estrategias, heurísticas y patrones.
- `QUERY_TEMPLATES.sql`: consultas SQL reutilizables.
- `backups/`: versiones históricas absorbidas para compatibilidad.

## Regla operativa
Si dudas entre dos enfoques, elige el que reduzca antes el espacio de búsqueda y retrase la apertura de contexto grande.
