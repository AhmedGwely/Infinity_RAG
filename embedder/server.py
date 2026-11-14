"""Shared BGE-M3 embedding service (CPU). Loaded once so API and worker do not each hold a 2+ GB model.

Vectors are produced exactly as the pre-existing index was built (same model, normalize_embeddings=True and an
explicit re-normalisation) so new and existing points live in the same vector space.
"""

import logging
import math
import os
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("embedder")
MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DIMENSION = 1024
state = {"status": "loading", "detail": "model is loading (first start downloads ~2.3 GB)", "model": None}
lock = threading.Lock()


def load_model() -> None:
    try:
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(int(os.getenv("EMBEDDER_THREADS", "6")))
        model = SentenceTransformer(MODEL_NAME, device="cpu")
        model.max_seq_length = int(os.getenv("EMBEDDER_MAX_SEQ", "1024"))
        model.encode(["warm-up"], normalize_embeddings=True)
        state.update(model=model, status="ok", detail=MODEL_NAME)
        log.info("model %s ready", MODEL_NAME)
    except Exception as error:  # report through /health instead of crashing the container
        log.exception("model load failed")
        state.update(status="error", detail=f"{type(error).__name__}: {str(error)[:200]}")


threading.Thread(target=load_model, daemon=True).start()
app = FastAPI(title="INFINITY embedder")


class EmbedBody(BaseModel):
    texts: list[str]
    kind: str = "document"


@app.get("/health")
def health():
    return {"status": state["status"], "detail": state["detail"], "dimension": DIMENSION}


@app.post("/embed")
def embed(body: EmbedBody):
    if state["status"] != "ok":
        raise HTTPException(503, state["detail"])
    if not body.texts or len(body.texts) > 64 or any(not t.strip() for t in body.texts):
        raise HTTPException(422, "texts must be 1-64 non-empty strings")
    with lock:  # one batch at a time keeps CPU/RAM predictable on a laptop
        rows = state["model"].encode(body.texts, batch_size=8, normalize_embeddings=True, convert_to_numpy=True).tolist()
    vectors = []
    for row in rows:
        if len(row) != DIMENSION:
            raise HTTPException(500, f"unexpected dimension {len(row)}")
        norm = math.sqrt(sum(v * v for v in row))
        if norm == 0 or not math.isfinite(norm):
            raise HTTPException(500, "invalid vector")
        vectors.append([v / norm for v in row])
    return {"vectors": vectors}
