"""O MCP do Bingus: busca e leitura de páginas como ferramentas para agentes, em /mcp."""

import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from bingus.api import db, llm, page
from bingus.api import search as engine
from bingus.common.hashing import content_hash, url_hash
from bingus.common.urls import normalize

MAX_TEXT = 20_000  # caracteres de uma página devolvidos de uma vez
# Hosts pelos quais o MCP é alcançado. Fora da lista o SDK responde 421 (DNS rebinding).
HOSTS = [h for h in os.environ.get("BINGUS_MCP_HOSTS", "localhost,127.0.0.1").split(",") if h]

server = MCPServer(
    "Bingus",
    instructions=(
        "Buscador da internet brasileira, em português. Use search para achar páginas sobre um"
        " assunto e read_page para ler uma página inteira, do índice ou buscada na hora."
    ),
)


@server.tool()
async def search(query: str, limit: int = 10) -> str:
    """Busca páginas da internet brasileira (notícias, governo, saúde, tecnologia, receitas,
    educação e muito mais). Escreva a busca em português, com as palavras que a página usaria.
    Devolve título, URL, data de publicação quando houver e um trecho de cada resultado."""
    res = await engine.run_search(query, max(1, min(limit, 20)), 0, summaries=False)
    lines = [f"Aviso: {w}" for w in res["warnings"]]
    if not res["results"]:
        lines.append(f"Nada encontrado no índice do Bingus para: {query}")
    for i, r in enumerate(res["results"], 1):
        when = f" · publicado em {r['published']}" if r["published"] else ""
        lines.append(f"{i}. {r['title'] or r['url']}\n   {r['url']}{when}")
        text = r["summary"] or r["snippet"]
        if text:
            lines.append("   " + " ".join(text.split()))
    return "\n".join(lines)


@server.tool()
async def read_page(url: str, question: str | None = None) -> str:
    """Lê uma página: título e texto extraído, sem menus nem propaganda. Funciona com URLs fora
    do índice, que são buscadas na hora. Com question, responde à pergunta usando só a página,
    quando o servidor tem uma LLM configurada."""
    canonical = normalize(url)
    if not canonical:
        return f"URL inválida: {url}"
    row = await db.pool().fetchrow(page.PAGE, url_hash(canonical))
    if row and row["text"]:
        title, text, digest, source = row["title"], row["text"], row["content_hash"], "índice"
    else:
        live = await page.fetch_live(canonical)
        await page.enqueue(canonical)
        if not live or not live["text"]:
            return f"Não consegui ler texto em {canonical}. A página entrou na fila do crawler."
        title, text, source = live["title"], live["text"], "buscada agora"
        canonical, digest = live["url"], content_hash(live["text"])
    out = [f"# {title or canonical}", f"{canonical} ({source})", ""]
    if question:
        answer = await llm.ask(digest, text, question)
        out += [f"Resposta: {answer}" if answer else "Sem LLM configurada para responder.", ""]
    out.append(text[:MAX_TEXT])
    if len(text) > MAX_TEXT:
        out.append(f"\n[texto cortado em {MAX_TEXT} de {len(text)} caracteres]")
    return "\n".join(out)


app = server.streamable_http_app(
    transport_security=TransportSecuritySettings(
        allowed_hosts=HOSTS + [f"{h}:*" for h in HOSTS],
    )
)
