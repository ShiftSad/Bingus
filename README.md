# Bingus

Buscador da internet brasileira em português, feito para virar um MCP de busca para agentes.

## Como funciona

- **Postgres** com [VectorChord](https://github.com/tensorchord/VectorChord): BM25 de verdade com
  stemmer em português e busca vetorial com IVF e RaBitQ, num banco só.
- **API** em Python, FastAPI: scheduler do crawler, ingestão, busca híbrida, sumários e telemetria.
- **Fetch workers**: baixam páginas, extraem texto, respeitam robots.txt e devolvem tudo à API.
  Rodam em qualquer máquina com uma chave de API, sem acesso ao banco.
- **Embed worker**: gera embeddings com `perplexity-ai/pplx-embed-v1-0.6b` numa GPU e serve
  embeddings de query para a busca.
- **Frontend**: Vite puro, chama a API direto.

A busca funde BM25, similaridade vetorial e PageRank por Reciprocal Rank Fusion. Páginas são
revisitadas com intervalo adaptativo, e só ganham embedding novo quando o texto muda de verdade.

## Rodando

```
cp .env.example .env
docker compose up -d                       # postgres, api, frontend
docker compose --profile workers up -d     # fetch e embed na mesma máquina, embed com GPU
```

Workers, em qualquer lugar:

```
docker build --build-arg EXTRA=fetch -t bingus-fetch bingus/
docker run -d -e BINGUS_API_URL=https://... -e BINGUS_API_KEY=... bingus-fetch bingus-fetch
```

PageRank, uma vez por dia: `docker compose exec api bingus-rank`.

Detalhes de arquitetura e decisões em `.claude/skills/bingus/SKILL.md`.
