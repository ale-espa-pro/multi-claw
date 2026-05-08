# Instrucciones operativas para agentes: memory_retrieval_tutorial

1. Clasifica primero la intención: dato concreto, resumen, estado/progreso, archivos/resultados, semántica abierta o literal del usuario.
2. Usa `multiagente.conversation_chunks` como índice semántico/operativo para localizar zonas útiles.
3. No trates `conversation_chunks` como transcripción fiel 1:1.
4. Empieza por retrieval compacto: prefijos anclados, gating estructural y longitud moderada.
5. Si hay restricción de tokens, prioriza **1 resultado por sesión** en la primera pasada.
6. Refina con texto (`ILIKE`/regex/FTS-BM25-like) solo dentro del subconjunto correcto.
7. Usa vectorial híbrida solo sobre subconjunto ya filtrado.
8. Reserva `multiagente.conversations.context_jsonb` para:
   - literal exacto del usuario
   - cronología real
   - atribución fiable por rol
   - verificación final
9. No abras `context_jsonb` completo como primera opción salvo necesidad real.
10. No dependas solo de `conversation_type` ni de nombres de proyectos ya vistos.
11. Trata el EXTRA DE MEMORIA AUTOMÁTICA como pista, no como verdad.
12. Si dudas, elige el enfoque que reduzca antes el espacio de búsqueda.
