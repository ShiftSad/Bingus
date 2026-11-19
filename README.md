# Bingus

Buscador da internet brasileira em português, com um MCP de busca para agentes.

## Como funciona

- **Postgres** com [VectorChord](https://github.com/tensorchord/VectorChord): BM25 de verdade com
  o tokenizador do Gemma e busca vetorial com IVF e RaBitQ, num banco só.
- **API** em Python, FastAPI: scheduler do crawler, ingestão, busca híbrida, MCP, sumários e telemetria.
- **Fetch workers**: baixam páginas, extraem texto, respeitam robots.txt e devolvem tudo à API.
  Rodam em qualquer máquina com uma chave de API, sem acesso ao banco.
- **Embed worker**: gera embeddings com `perplexity-ai/pplx-embed-v1-0.6b` numa GPU e serve
  embeddings de query para a busca.
- **Frontend**: Vite puro, chama a API direto.

A busca funde BM25, similaridade vetorial e PageRank por Reciprocal Rank Fusion, com no máximo dois
resultados por site no topo. Páginas são revisitadas com intervalo adaptativo, e só ganham embedding
novo quando o texto muda de verdade. Texto repetido em várias URLs é embedado uma vez só.

## MCP

A API serve um MCP em `/mcp` com duas ferramentas: `search`, a busca híbrida, e `read_page`, que lê
uma página do índice ou a busca na hora. No Claude Code:

```
claude mcp add --transport http bingus http://10.10.0.50:2026/mcp
```

O endereço precisa estar em `BINGUS_MCP_HOSTS`, senão o SDK responde 421.

## Rodando

```
cp .env.example .env
docker compose up -d                       # postgres, api, frontend
docker compose --profile workers up -d     # fetch e embed na mesma máquina, embed com GPU
```

Numa máquina compartilhada, instale antes o teto de recursos e os timers de manutenção, como diz o
cabeçalho de `systemd/bingus.slice`. Sementes variadas em `seeds/brasil.txt`, via `POST /fetch/seed`.

Workers, em qualquer lugar:

```
docker build --build-arg EXTRA=fetch -t bingus-fetch bingus/
docker run -d -e BINGUS_API_URL=https://... -e BINGUS_API_KEY=... bingus-fetch bingus-fetch
```

PageRank, uma vez por dia: `docker compose exec api bingus-rank` (o timer `bingus-diario` faz isso).

Detalhes de arquitetura e decisões em `.claude/skills/bingus/SKILL.md`.
