import asyncio
import logging
import os

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

from bingus.common.client import Api
from bingus.common.stats import Stats

log = logging.getLogger("bingus.embed")

MODEL = os.environ.get("BINGUS_EMBED_MODEL", "perplexity-ai/pplx-embed-v1-0.6b")
DIMS = 512  # Matryoshka: metade do modelo, metade do armazenamento
TOKENS = int(os.environ.get("BINGUS_EMBED_TOKENS", "2048"))
BATCH = int(os.environ.get("BINGUS_EMBED_BATCH", "8"))  # chunks por passada na GPU
LEASE = int(os.environ.get("BINGUS_EMBED_LEASE", "64"))  # chunks por lote da API
PORT = int(os.environ.get("BINGUS_EMBED_PORT", "8100"))  # queries de busca


class Embedder:
    def __init__(self) -> None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # fp16 estoura e vira NaN neste modelo. bf16 só existe a partir de Ampere; Pascal usa fp32.
        ampere = device == "cuda" and torch.cuda.get_device_capability()[0] >= 8
        dtype = torch.bfloat16 if ampere else torch.float32
        self.model = SentenceTransformer(
            MODEL,
            device=device,
            trust_remote_code=True,
            truncate_dim=DIMS,
            model_kwargs={"torch_dtype": dtype},
        )
        self.model.max_seq_length = TOKENS
        self.gpu = asyncio.Lock()  # queries e lotes se revezam; uma query espera no máximo BATCH
        log.info("model on %s %s, %d dims, %d tokens", device, dtype, DIMS, TOKENS)

    async def encode(self, texts: list[str]) -> list[bytes]:
        async with self.gpu:
            vecs = await asyncio.to_thread(
                self.model.encode, texts, batch_size=BATCH, show_progress_bar=False
            )
        return [np.asarray(v).astype(np.int8).tobytes() for v in vecs]


async def work(api: Api, emb: Embedder, stats: Stats) -> None:
    while True:
        try:
            chunks = (await api.post_json(f"/embed/batch?size={LEASE}", {}))["chunks"]
            stats.requests += 1
            if not chunks:
                await asyncio.sleep(15)
                continue
            out = []
            for i in range(0, len(chunks), BATCH):
                part = chunks[i : i + BATCH]
                vecs = await emb.encode([c["text"] for c in part])
                out += [
                    {"page_id": c["page_id"], "seq": c["seq"], "embedding": v}
                    for c, v in zip(part, vecs, strict=True)
                ]
            await api.post_msgpack("/embed/results", {"chunks": out})
            stats.requests += 1
            stats.pages += len(out)
            stats.bytes += sum(len(c["text"]) for c in chunks)
            log.info("%d chunks", len(out))
        except Exception as e:
            log.warning("batch failed: %s", e)
            stats.error("other")
            await asyncio.sleep(30)


class Query(BaseModel):
    texts: list[str]


def query_app(emb: Embedder) -> FastAPI:
    """Embeddings para a busca. Sem auth: só é alcançável pela rede interna."""
    app = FastAPI(title="Bingus embed")

    @app.post("/embed")
    async def embed(q: Query):
        vecs = await emb.encode(q.texts)
        return {"embeddings": [memoryview(v).cast("b").tolist() for v in vecs]}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    api = Api(os.environ["BINGUS_API_URL"], os.environ["BINGUS_API_KEY"])
    stats = Stats()
    emb = Embedder()
    server = uvicorn.Server(
        uvicorn.Config(query_app(emb), host="0.0.0.0", port=PORT, log_level="warning")
    )

    async def run() -> None:
        await asyncio.gather(work(api, emb, stats), stats.run(api), server.serve())

    asyncio.run(run())
