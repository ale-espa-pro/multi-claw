"""
Semantic splitter optimizado:
 - UNA sola ronda de embeddings para todas las oraciones de todos los textos
 - UNA sola ronda de embeddings para todos los chunks finales
 - Cada ronda se despacha en batches de ~250K tokens × 8 hilos en paralelo
 - Resultado devuelto como numpy arrays (embeddings) + listas ligeras (textos/offsets)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import tiktoken
from openai import OpenAI
from tqdm import tqdm

# ── Configuración ─────────────────────────────────────────────────

SEPARATORS_DEFAULT = [
    "\n\n", "\n", ". ", "? ", "! ", "; ", ", ", "]",
]

MAX_WORKERS = 2

# ── Tokenizador (singleton) ──────────────────────────────────────

_ENCODER: tiktoken.Encoding | None = None


def _get_encoder() -> tiktoken.Encoding:
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def count_tokens(text: str, encoder: tiktoken.Encoding | None = None) -> int:
    return len((encoder or _get_encoder()).encode(text))


# ── Estructuras de salida optimizadas ─────────────────────────────

@dataclass(slots=True)
class SplitResultOpt:
    """
    Resultado por texto de entrada.
      - parent_idx:       índice del texto original
      - chunk_texts:      list[str]           textos de cada chunk
      - chunk_tokens:     np.ndarray[int32]   tokens por chunk
      - chunk_embeddings: np.ndarray[float32, (n_chunks, dim)]  embeddings
      - chunk_spans:      np.ndarray[int32, (n_chunks, 2)]
                          (char_start, char_end) de cada chunk en parent_text
    """
    parent_idx: int
    parent_text: str
    chunk_texts: list[str]
    chunk_tokens: np.ndarray          # int32
    chunk_embeddings: np.ndarray      # float32  (n_chunks, dim)
    chunk_spans: np.ndarray           # int32    (n_chunks, 2)  [start, end)


# ── Embeddings masivos en paralelo ────────────────────────────────

MAX_TOKENS_PER_BATCH = 80_000  # margen de seguridad vs el hard limit de 300K
MAX_TOKENS_PER_INPUT = 7_500
EMBED_MODEL = "text-embedding-3-large"  # o el modelo que uses
MAX_ITEMS_PER_BATCH = 2048


def split_text_by_tokens(
    text: str,
    encoder: tiktoken.Encoding,
    max_tokens: int = MAX_TOKENS_PER_INPUT,
) -> list[str]:
    token_ids = encoder.encode(text)
    if len(token_ids) <= max_tokens:
        return [text]

    chunks: list[str] = []
    for start in range(0, len(token_ids), max_tokens):
        chunk = encoder.decode(token_ids[start:start + max_tokens])
        if chunk.strip():
            chunks.append(chunk)
    return chunks

def _split_into_batches(
    texts: list[str],
    encoder: tiktoken.Encoding,
    max_tokens: int = MAX_TOKENS_PER_BATCH,
    max_items: int = MAX_ITEMS_PER_BATCH,
) -> list[list[int]]:
    batches: list[list[int]] = []
    cur_batch: list[int] = []
    cur_tokens = 0
    for i, text in enumerate(texts):
        n = len(encoder.encode(text))
        if cur_batch and (cur_tokens + n > max_tokens or len(cur_batch) >= max_items):
            batches.append(cur_batch)
            cur_batch, cur_tokens = [], 0
        cur_batch.append(i)
        cur_tokens += n
    if cur_batch:
        batches.append(cur_batch)
    return batches

def _embed_batch(texts: list[str], client: OpenAI) -> list[list[float]]:
    """Llama al endpoint de embeddings para un batch de textos."""
    if not texts:
        return []
    resp = client.embeddings.create(
        input=texts,
        model=EMBED_MODEL,
    )
    # Ordenar por index para garantizar el orden
    return [e.embedding for e in sorted(resp.data, key=lambda x: x.index)]

def get_embeddings_parallel(
    texts: list[str],
    client: OpenAI,
    encoder: tiktoken.Encoding | None = None,
) -> np.ndarray:
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    
    # Reemplazar vacíos para evitar error de API
    texts = [t if t.strip() else " " for t in texts]
    
    enc = encoder or _get_encoder()
    oversized = []
    for idx, text in enumerate(texts):
        token_count = len(enc.encode(text))
        if token_count > MAX_TOKENS_PER_INPUT:
            oversized.append((idx, token_count))
    if oversized:
        idx, token_count = oversized[0]
        raise ValueError(
            f"embedding input {idx} has {token_count} tokens; "
            f"split it before embedding (max {MAX_TOKENS_PER_INPUT})"
        )

    batch_indices = _split_into_batches(texts, enc)

    # Pre-alocar resultado; se llena in-place para evitar copias
    dim: int | None = None
    raw: dict[int, list[list[float]]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_embed_batch, [texts[j] for j in idxs], client): (bi, idxs)
            for bi, idxs in enumerate(batch_indices)
        }
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Embeddings", leave=False):
            bi, idxs = futures[fut]
            embs = fut.result()
            if dim is None:
                dim = len(embs[0])
            raw[bi] = (idxs, embs)

    # Montar array final en orden
    out = np.empty((len(texts), dim), dtype=np.float32)
    for bi in sorted(raw):
        idxs, embs = raw[bi]
        for local, global_idx in enumerate(idxs):
            out[global_idx] = embs[local]
    return out


# ── Similitud coseno vectorizada ──────────────────────────────────

def cosine_similarities(embeddings: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)  # evitar div/0
    normed = embeddings / norms
    return np.einsum("ij,ij->i", normed[:-1], normed[1:])


# ── Splitting semántico (sin cambios lógicos) ─────────────────────

def split_into_sentences(
    text: str,
    separators: list[str] | str = SEPARATORS_DEFAULT,
    *,
    encoder: tiktoken.Encoding | None = None,
    max_segment_tokens: int = MAX_TOKENS_PER_INPUT,
) -> tuple[list[str], list[tuple[int, int]]]:
    """
    Divide texto en segmentos base y devuelve (sentences, spans).
    Cada span es (char_start, char_end) en el texto original.
    """
    if isinstance(separators, str):
        separators = [separators]
    separators = sorted(separators, key=len, reverse=True)
    pattern = "|".join(re.escape(s) for s in separators)

    sentences: list[str] = []
    spans: list[tuple[int, int]] = []
    last_end = 0
    enc = encoder or _get_encoder()

    def append_segment(seg: str, absolute_start: int):
        stripped = seg.strip()
        if not stripped:
            return

        left_pad = len(seg) - len(seg.lstrip())
        segment_start = absolute_start + left_pad
        segment_end = segment_start + len(stripped)
        token_pieces = split_text_by_tokens(stripped, enc, max_segment_tokens)

        if len(token_pieces) == 1:
            sentences.append(stripped)
            spans.append((segment_start, segment_end))
            return

        cursor = segment_start
        for piece in token_pieces:
            piece_text = piece.strip()
            if not piece_text:
                cursor += len(piece)
                continue
            piece_start = cursor + max(0, piece.find(piece_text))
            piece_end = min(segment_end, piece_start + len(piece_text))
            sentences.append(piece_text)
            spans.append((piece_start, piece_end))
            cursor += len(piece)

    for m in re.finditer(pattern, text):
        seg = text[last_end:m.start()]
        append_segment(seg, last_end)
        last_end = m.end()

    # Último segmento tras el último separador
    seg = text[last_end:]
    append_segment(seg, last_end)

    return sentences, spans


def find_split_points(similarities: np.ndarray, threshold_percentile: int = 25) -> list[int]:
    threshold = np.percentile(similarities, threshold_percentile)
    return (np.where(similarities < threshold)[0] + 1).tolist()


def _tokens_of_group(sentences: list[str], encoder: tiktoken.Encoding) -> int:
    return count_tokens(" ".join(sentences), encoder)


def group_sentences(
    sentences: list[str],
    split_points: list[int],
    encoder: tiktoken.Encoding,
    *,
    sentence_spans: list[tuple[int, int]] | None = None,
    min_tokens: int = 50,
    max_tokens: int | None = None,
    overlap_tokens: int = 0,
) -> tuple[list[str], list[tuple[int, int]]]:
    """
    Agrupa oraciones en chunks. Devuelve (chunk_texts, chunk_spans).
    chunk_spans[i] = (char_start, char_end) en el parent_text original.
    """
    # Trabajar con índices para rastrear spans
    n = len(sentences)
    all_idxs = list(range(n))

    # 1. Corte base
    raw_groups: list[list[int]] = []
    prev = 0
    for pt in split_points:
        g = all_idxs[prev:pt]
        if g:
            raw_groups.append(g)
        prev = pt
    tail = all_idxs[prev:]
    if tail:
        raw_groups.append(tail)

    # 2. Fusionar pequeños
    merged: list[list[int]] = []
    buf: list[int] = []
    for g in raw_groups:
        buf.extend(g)
        if _tokens_of_group([sentences[j] for j in buf], encoder) >= min_tokens:
            merged.append(buf)
            buf = []
    if buf:
        if merged:
            merged[-1].extend(buf)
        else:
            merged.append(buf)

    # 3. Partir grandes
    if max_tokens:
        split_g: list[list[int]] = []
        for g in merged:
            cur: list[int] = []
            for idx in g:
                cand = cur + [idx]
                if _tokens_of_group([sentences[j] for j in cand], encoder) > max_tokens and cur:
                    split_g.append(cur)
                    cur = [idx]
                else:
                    cur = cand
            if cur:
                split_g.append(cur)
        merged = split_g

    # 4. Overlap
    if overlap_tokens > 0 and len(merged) > 1:
        overlapped: list[list[int]] = [merged[0]]
        for i in range(1, len(merged)):
            prev_g = merged[i - 1]
            tail_idxs: list[int] = []
            tail_tok = 0
            for idx in reversed(prev_g):
                st = count_tokens(sentences[idx], encoder)
                if tail_tok + st > overlap_tokens:
                    break
                tail_idxs.insert(0, idx)
                tail_tok += st
            overlapped.append(tail_idxs + merged[i])
        merged = overlapped

    # Construir textos y spans
    chunk_texts = [" ".join(sentences[j] for j in g) for g in merged]

    if sentence_spans is not None:
        chunk_spans = [
            (sentence_spans[g[0]][0], sentence_spans[g[-1]][1])
            for g in merged
        ]
    else:
        chunk_spans = [(0, 0)] * len(merged)

    return chunk_texts, chunk_spans


# ── Función principal optimizada ──────────────────────────────────

def semantic_split(
    texts: list[str],
    client: OpenAI,
    *,
    separators: list[str] | str = SEPARATORS_DEFAULT,
    threshold_percentile: int = 25,
    min_tokens: int = 50,
    max_tokens: int | None = None,
    overlap_tokens: int = 0,
) -> list[SplitResultOpt]:
    """
    Optimizado: agrupa TODAS las oraciones de todos los textos en una sola
    ronda de embeddings paralelos, y luego TODOS los chunks en otra ronda.
    Devuelve numpy arrays float32 en vez de listas de floats.
    """
    texts = [t if t.strip() else " " for t in texts]
    encoder = _get_encoder()
    max_segment_tokens = min(max_tokens or MAX_TOKENS_PER_INPUT, MAX_TOKENS_PER_INPUT)
    # ─── Fase 1: dividir todos los textos en oraciones ───
    per_text: list[dict] = []  # {sentences, spans, trivial, text}
    all_sentences: list[str] = []
    sentence_ranges: list[tuple[int, int]] = []  # (start, end) en all_sentences

    for idx, text in enumerate(texts):
        sents, spans = split_into_sentences(
            text,
            separators,
            encoder=encoder,
            max_segment_tokens=max_segment_tokens,
        )
        trivial = len(sents) <= 2 and count_tokens(text, encoder) <= max_segment_tokens
        start = len(all_sentences)

        if trivial:
            all_sentences.append(text)  # embed el texto completo
        else:
            all_sentences.extend(sents)

        end = len(all_sentences)
        sentence_ranges.append((start, end))
        per_text.append({
            "sentences": sents, "spans": spans,
            "trivial": trivial, "text": text, "idx": idx,
        })

    # ─── Fase 2: UNA ronda de embeddings para todas las oraciones ───
    all_sent_embs = get_embeddings_parallel(all_sentences, client, encoder)

    # ─── Fase 3: agrupar chunks por cada texto ───
    all_chunk_texts: list[str] = []
    all_chunk_spans: list[tuple[int, int]] = []
    chunk_ranges: list[tuple[int, int]] = []  # (start, end) en all_chunk_texts

    for i, info in enumerate(per_text):
        s_start, s_end = sentence_ranges[i]

        if info["trivial"]:
            # Un solo chunk = el texto completo (ya embebido)
            c_start = len(all_chunk_texts)
            all_chunk_texts.append(info["text"])
            all_chunk_spans.append((0, len(info["text"])))
            chunk_ranges.append((c_start, c_start + 1))
            # Marcar para reusar el embedding de la fase 2
            info["_reuse_emb_idx"] = s_start
            continue

        # Extraer embeddings de oraciones de este texto
        sent_embs = all_sent_embs[s_start:s_end]
        sims = cosine_similarities(sent_embs)
        split_pts = find_split_points(sims, threshold_percentile)
        chunk_txts, chunk_spns = group_sentences(
            info["sentences"], split_pts, encoder,
            sentence_spans=info["spans"],
            min_tokens=min_tokens, max_tokens=max_tokens, overlap_tokens=overlap_tokens,
        )

        c_start = len(all_chunk_texts)
        all_chunk_texts.extend(chunk_txts)
        all_chunk_spans.extend(chunk_spns)
        chunk_ranges.append((c_start, c_start + len(chunk_txts)))
        info["_chunk_texts"] = chunk_txts

    # ─── Fase 4: UNA ronda de embeddings para todos los chunks ───
    # Excluir los triviales (ya tienen embedding)
    non_trivial_indices: list[int] = []
    non_trivial_texts: list[str] = []
    trivial_map: dict[int, int] = {}  # chunk_global_idx -> sent_emb_idx

    for i, info in enumerate(per_text):
        c_start, c_end = chunk_ranges[i]
        if info["trivial"]:
            trivial_map[c_start] = info["_reuse_emb_idx"]
        else:
            for j in range(c_start, c_end):
                non_trivial_indices.append(j)
                non_trivial_texts.append(all_chunk_texts[j])

    # Embed solo los no-triviales
    dim = all_sent_embs.shape[1]
    all_chunk_embs = np.empty((len(all_chunk_texts), dim), dtype=np.float32)

    if non_trivial_texts:
        nt_embs = get_embeddings_parallel(non_trivial_texts, client, encoder)
        for local, global_idx in enumerate(non_trivial_indices):
            all_chunk_embs[global_idx] = nt_embs[local]

    # Copiar embeddings reutilizados de triviales
    for c_idx, s_idx in trivial_map.items():
        all_chunk_embs[c_idx] = all_sent_embs[s_idx]

    # ─── Fase 5: construir resultados ───
    results: list[SplitResultOpt] = []
    for i, info in enumerate(per_text):
        c_start, c_end = chunk_ranges[i]
        c_texts = all_chunk_texts[c_start:c_end]
        c_embs = all_chunk_embs[c_start:c_end]  # vista, no copia
        c_tokens = np.array(
            [count_tokens(t, encoder) for t in c_texts], dtype=np.int32,
        )
        c_spans = np.array(
            all_chunk_spans[c_start:c_end], dtype=np.int32,
        )  # (n_chunks, 2)
        results.append(SplitResultOpt(
            parent_idx=i,
            parent_text=info["text"],
            chunk_texts=c_texts,
            chunk_tokens=c_tokens,
            chunk_embeddings=c_embs.copy(),
            chunk_spans=c_spans,
        ))

    return results
