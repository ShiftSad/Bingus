import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bingus.api import db, embed, fetch, metrics, page, search, seed, tools


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.connect()
    sampler = asyncio.create_task(metrics.sample_loop())
    async with tools.server.session_manager.run():  # app montada não roda o próprio lifespan
        yield
    sampler.cancel()
    await db.close()


app = FastAPI(title="Bingus", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])  # busca no browser
app.include_router(fetch.router)
app.include_router(embed.router)
app.include_router(metrics.router)
app.include_router(search.router)
app.include_router(page.router)
app.include_router(seed.router)


@app.get("/health")
async def health():
    return {"ok": True}


app.mount("/", tools.app)  # MCP em /mcp; por último, porque a montagem casa com qualquer caminho


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
