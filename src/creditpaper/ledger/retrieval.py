"""Time-scoped, source-anchored keyword and vector retrieval.

Embeddings are derived indexes, supplied by a configured worker, never facts.
The portable exact-search implementation has a hard corpus bound and reports
that bound. It never silently pretends a partial search covered the whole deal.
"""

from __future__ import annotations

import math

from creditpaper.db import tenant_cursor
from creditpaper.ledger.service import digest
from creditpaper.ledger.types import BadInput, Conflict, NotFound, QueryScope
from creditpaper.processing.chunker import chunk_text


def page_index_chunks(pages: list[dict]) -> list[dict]:
    """Build retrieval chunks in Python; shared by bulk and legacy persistence."""
    rows = []
    for page in pages:
        number = page.get("page")
        if not isinstance(number, int) or number < 1:
            continue
        location = page.get("sheet_name") or f"page {number}"
        for chunk in chunk_text(page.get("text", "")):
            rows.append(
                {
                    "page": number,
                    "location": location,
                    "chunk_index": chunk["idx"],
                    "content": chunk["text"],
                    "content_hash": digest(chunk["text"]),
                }
            )
    return rows


def index_pages(cur, tenant_id: str, deal_id: str, version_id: str, pages: list[dict]) -> None:
    rows = [
        (
            tenant_id,
            deal_id,
            version_id,
            c["page"],
            c["location"],
            c["chunk_index"],
            c["content"],
            c["content_hash"],
        )
        for c in page_index_chunks(pages)
    ]
    if rows:
        cur.executemany(
            """INSERT INTO ledger_chunks
            (tenant_id,deal_id,version_id,page,location,chunk_index,content,content_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            rows,
        )


def validate_vector(vector: list[float]) -> None:
    if not 1 <= len(vector) <= 4096 or not all(math.isfinite(v) for v in vector):
        raise BadInput("Embedding must contain 1 to 4096 finite numbers")
    if math.hypot(*vector) == 0:
        raise BadInput("Embedding must have nonzero magnitude")


def store_embedding(tenant_id: str, chunk_id: str, model: str, vector: list[float]) -> None:
    validate_vector(vector)
    if not 1 <= len(model) <= 200:
        raise BadInput("Embedding model identifier required")
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT 1 FROM ledger_chunks WHERE tenant_id=%s AND id=%s", (tenant_id, chunk_id))
        if not cur.fetchone():
            raise NotFound("Document chunk not found")
        cur.execute(
            """INSERT INTO ledger_embeddings(tenant_id,chunk_id,model,embedding,dimensions)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING chunk_id""",
            (tenant_id, chunk_id, model, vector, len(vector)),
        )
        if not cur.fetchone():
            cur.execute(
                "SELECT embedding FROM ledger_embeddings WHERE tenant_id=%s AND chunk_id=%s AND model=%s",
                (tenant_id, chunk_id, model),
            )
            if cur.fetchone()[0] != vector:
                raise Conflict("Embedding model revisions require a new identifier")


def search_documents(
    tenant_id: str,
    deal_id: str,
    query: str,
    scope: QueryScope,
    *,
    vector: list[float] | None = None,
    model: str | None = None,
    limit: int = 10,
) -> dict:
    if not query.strip() or len(query) > 2000 or not 1 <= limit <= 50:
        raise BadInput("Provide a query of at most 2000 characters and a limit of 1 to 50")
    if vector is not None:
        validate_vector(vector)
        if not model:
            raise BadInput("Vector search requires an embedding model identifier")
    with tenant_cursor(tenant_id) as cur:
        cur.execute("SELECT 1 FROM deals WHERE tenant_id=%s AND id=%s", (tenant_id, deal_id))
        if not cur.fetchone():
            raise NotFound("Deal not found")
        if vector is None:
            return _ranked_search(cur, tenant_id, deal_id, query, scope, limit=limit)
        cur.execute("""SELECT 1 FROM information_schema.columns WHERE table_schema='public'
            AND table_name='ledger_embeddings' AND column_name='embedding_vector'""")
        if cur.fetchone():
            return _ranked_search(
                cur, tenant_id, deal_id, query, scope, limit=limit, vector=vector, model=model
            )
        # Chunk versions remain queryable after reprocessing. Select the last indexed
        # content per page/offset known at this query's cutoff.
        cur.execute(
            """WITH eligible AS (
            SELECT DISTINCT ON(c.version_id,c.page,c.chunk_index)
              c.id,c.version_id,c.page,c.location,c.content,c.search,c.recorded_at
            FROM ledger_chunks c JOIN deal_document_versions v
              ON v.tenant_id=c.tenant_id AND v.id=c.version_id
            JOIN deal_documents d ON d.id=v.document_id AND d.tenant_id=c.tenant_id
            WHERE c.tenant_id=%s AND c.deal_id=%s AND c.recorded_at<=%s AND v.uploaded_at<=%s
              AND (v.superseded_at IS NULL OR v.superseded_at>%s)
              AND (d.archived_at IS NULL OR d.archived_at>%s)
            ORDER BY c.version_id,c.page,c.chunk_index,c.recorded_at DESC,c.id
          ) SELECT e.id,e.version_id,e.page,e.location,e.content,
              ts_rank_cd(e.search,websearch_to_tsquery('english',%s)) AS keyword_score,
              v.embedding
            FROM eligible e LEFT JOIN ledger_embeddings v ON v.tenant_id=%s AND v.chunk_id=e.id
              AND v.model=%s AND v.recorded_at<=%s
            ORDER BY e.id LIMIT 5001""",
            (
                tenant_id,
                deal_id,
                scope.known_at,
                scope.known_at,
                scope.known_at,
                scope.known_at,
                query,
                tenant_id,
                model,
                scope.known_at,
            ),
        )
        rows = cur.fetchall()
        if len(rows) > 5000:
            raise Conflict(
                "Search corpus exceeds the portable index limit; configure the vector search index"
            )
        keyword = sorted((r for r in rows if r[5] > 0), key=lambda r: (-r[5], str(r[0])))
        semantic = []
        if vector is not None:
            norm = math.hypot(*vector)
            for row in rows:
                embedding = row[6]
                if embedding is not None and len(embedding) == len(vector):
                    score = sum(a * b for a, b in zip(vector, embedding, strict=True)) / (
                        norm * math.hypot(*embedding)
                    )
                    semantic.append((score, row))
            semantic.sort(key=lambda item: (-item[0], str(item[1][0])))
        scores = {}
        for ranked in (keyword, [r for _, r in semantic]):
            for rank, row in enumerate(ranked[:100], 1):
                scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank)
        by_id = {r[0]: r for r in rows}
        results = []
        for chunk_id in sorted(scores, key=lambda key: (-scores[key], str(key)))[:limit]:
            row = by_id[chunk_id]
            results.append(
                {
                    "chunk_id": str(row[0]),
                    "version_id": str(row[1]),
                    "page": row[2],
                    "location": row[3],
                    "text": row[4],
                    "score": scores[chunk_id],
                }
            )
        return {
            "results": results,
            "mode": "hybrid" if vector is not None else "keyword",
            "embedding_coverage": len(semantic),
            "eligible_chunks": len(rows),
            "scope": scope.model_dump(mode="json"),
        }


def _ranked_search(cur, tenant_id, deal_id, query, scope, *, limit, vector=None, model=None):
    # Fixed SQL; only the validated integer vector dimension is interpolated.
    eligible = """c.tenant_id=%s AND c.deal_id=%s AND c.recorded_at<=%s AND v.uploaded_at<=%s
        AND (v.superseded_at IS NULL OR v.superseded_at>%s)
        AND (d.archived_at IS NULL OR d.archived_at>%s)
        AND NOT EXISTS(SELECT 1 FROM ledger_chunks newer WHERE newer.tenant_id=c.tenant_id
            AND newer.version_id=c.version_id AND newer.page=c.page AND newer.chunk_index=c.chunk_index
            AND newer.recorded_at<=%s AND (newer.recorded_at,newer.id)>(c.recorded_at,c.id))"""
    params = (
        tenant_id,
        deal_id,
        scope.known_at,
        scope.known_at,
        scope.known_at,
        scope.known_at,
        scope.known_at,
    )
    joins = """FROM ledger_chunks c
        JOIN deal_document_versions v ON v.tenant_id=c.tenant_id AND v.id=c.version_id
        JOIN deal_documents d ON d.tenant_id=c.tenant_id AND d.id=v.document_id"""
    columns = "c.id,c.version_id,c.page,c.location,c.content"
    cur.execute(
        f"""SELECT {columns},ts_rank_cd(c.search,websearch_to_tsquery('english',%s)) score
        {joins} WHERE {eligible} AND c.search @@ websearch_to_tsquery('english',%s)
        ORDER BY score DESC,c.id LIMIT 100""",
        (query, *params, query),
    )
    keyword = cur.fetchall()
    semantic = []
    if vector is not None:
        dimensions = len(vector)
        cur.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
        cur.execute(
            f"""SELECT {columns},1-(e.embedding_vector::vector({dimensions}) <=> %s::vector({dimensions}))
            {joins} JOIN ledger_embeddings e ON e.tenant_id=c.tenant_id AND e.chunk_id=c.id
            WHERE {eligible} AND e.model=%s AND e.dimensions=%s AND e.recorded_at<=%s
            ORDER BY e.embedding_vector::vector({dimensions}) <=> %s::vector({dimensions}) LIMIT 100""",
            (str(vector), *params, model, dimensions, scope.known_at, str(vector)),
        )
        semantic = cur.fetchall()
    scores, rows = {}, {}
    for ranked in (keyword, semantic):
        for rank, row in enumerate(ranked, 1):
            rows[row[0]] = row
            scores[row[0]] = scores.get(row[0], 0) + 1 / (60 + rank)
    result = []
    for cid in sorted(scores, key=lambda key: (-scores[key], str(key)))[:limit]:
        row = rows[cid]
        result.append(
            {
                "chunk_id": str(cid),
                "version_id": str(row[1]),
                "page": row[2],
                "location": row[3],
                "text": row[4],
                "score": scores[cid],
            }
        )
    return {
        "results": result,
        "mode": "hybrid" if vector is not None else "keyword",
        "semantic_candidates": len(semantic),
        "approximate": vector is not None,
        "scope": scope.model_dump(mode="json"),
    }
