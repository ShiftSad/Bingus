---
name: bingus
description: Como trabalhar no Bingus, o buscador da internet brasileira. Leia antes de tocar em qualquer arquivo do projeto. Regras de código, arquitetura, decisões fechadas, schema e protocolo entre API e workers.
---

# Bingus

Buscador da internet brasileira em português. O produto final é um MCP de busca para
agentes. O núcleo é um crawler distribuído em Python que alimenta um Postgres com
BM25 e busca vetorial.

## Como trabalhar

- Código simples, poucos comentários, fácil de ler. Comentário só onde o porquê não é óbvio.
- O projeto se mantém pequeno. Nada de abstração para o futuro, nada de camada extra.
- Commits em português, curtos, sem trailer de coautoria. Push quando o servidor precisa puxar.
- Siga devagar. Uma peça por vez, mostre, espere o ok.
- O backend NestJS foi apagado. O frontend Vite está congelado, exceto `src/search.ts`.
- Sem testes por enquanto. Ruff para lint e formatação.
- Idioma do código: inglês. Idioma das conversas e da documentação: português.
- Tudo Python com uv. FastAPI, asyncpg, SQL escrito à mão. Sem ORM. Sem migrations: um `schema.sql` só.
- O banco de produção vive no dedicado e não é mais descartável. Mudança de schema é feita à mão
  lá, com `ALTER` e `CREATE INDEX CONCURRENTLY`, e espelhada no `schema.sql`.

## Arquitetura

Quatro peças. Só a API fala com o banco.

```
postgres  <->  api  <-- HTTP + API key -->  fetch workers (VPSs)
                                        -->  embed workers (GPU)
```

- `api`: FastAPI. Scheduler, ingestão dos resultados, busca, métricas no Postgres, seeds.
- `fetch`: baixa páginas, extrai texto com trafilatura, acha links, devolve tudo à API.
- `embed`: gera embeddings dos chunks na GPU e devolve à API.
- `common`: URL, hashes, simhash, chunking, cliente HTTP. Tudo que dois entrypoints usam.

Layout: `bingus/` é o pacote. Entrypoints `bingus-api`, `bingus-fetch`, `bingus-embed`.
Um Dockerfile só, com `--build-arg EXTRA=api|fetch|embed` escolhendo as dependências e o
comando escolhendo o modo. Workers sobem soltos, recebendo `BINGUS_API_URL` e `BINGUS_API_KEY`.
Não precisam de mais nada.

```
docker build --build-arg EXTRA=fetch -t bingus-fetch bingus/
docker run -d -e BINGUS_API_URL=https://... -e BINGUS_API_KEY=... bingus-fetch bingus-fetch
```

Fetch worker aceita ainda `BINGUS_HOSTS`, hosts em voo, `BINGUS_PER_HOST`, `BINGUS_FLUSH` e
`BINGUS_USER_AGENT`. Localmente: `uv run bingus-fetch` dentro de `bingus/`.

## Infra

- Dedicado Ubuntu Server 22 atrás de WireGuard: Postgres, API, embed worker com 1050 Ti (4 GB).
- VPS de entrada com Caddy e TLS na frente da API.
- Fetch workers em VPSs na Alemanha e no Brasil.
- PC Windows com 3060 Ti roda embed worker nativo via `uv run bingus-embed`. Nada de Docker no Windows.
- Recursos do dedicado: 16 a 24 GB de RAM, cerca de 4 núcleos. Eficiente, sem obsessão.
- Alvo: 10 milhões de páginas em cerca de 50 GB. Guia, não teto.

## Modelo de embedding

`perplexity-ai/pplx-embed-v1-0.6b`. 1024 dims, Matryoshka, saída int8 sem normalização,
similaridade por cosseno, sem prefixo de instrução, contexto 32K.

- Truncar para 512 dims. Enviar int8 cru, 512 bytes por vetor.
- fp16 dá NaN neste modelo. bf16 em Ampere ou mais nova, fp32 na 1050 Ti, escolhido sozinho.
  A saída do modelo já é int8, entregue como float32 com valores inteiros.
- 2048 tokens por chunk, batch 8 na 1050 Ti. `BINGUS_EMBED_TOKENS` e `BINGUS_EMBED_BATCH`.
  `BINGUS_EMBED_LEASE` é quantos chunks pega da API por vez; 256 mantém a GPU sem vales.
- Medido na 3060 Ti com chunks da Wikipédia: 5 chunks por segundo, batch 16 ou 32 dá no mesmo,
  a GPU fica a 100% e usa 7 GB de VRAM com batch 32. É limite de computação do modelo, não de
  configuração. Um milhão de chunks leva uns dois dias e meio nessa placa.
- Query e documento usam o mesmo modelo sem prefixo.
- O embed worker serve `POST /embed` na porta `BINGUS_EMBED_PORT`, padrão 8100, para queries de
  busca. Sem auth, só na rede interna. Query e lote se revezam num lock: a query espera no
  máximo um mini-batch. Corpo `{"texts": [...]}`, resposta int8 em listas.
- Torch vem do índice CUDA 12.6 do PyTorch, o último que roda na 1050 Ti. Configurado no
  `pyproject.toml`. No Windows a instalação é a mesma, `uv sync --extra embed`.
- Fluxo: `POST /embed/batch?size=64` aluga chunks por 15 minutos e devolve o texto de cada um.
  A lease pega páginas em ordem de PageRank, pelo índice `pages_rank`: com backlog grande, a
  busca vetorial passa a enxergar primeiro o que mais importa. 2 ms por lease, 200 ms no pior caso.
  `POST /embed/results` recebe msgpack com bytes crus e grava com `quantize_to_rabitq8`.

## Banco

Imagem `tensorchord/vchord-suite:pg18-latest`. Extensões `vchord`, `pg_tokenizer`, `vchord_bm25`.
Schema em `bingus/schema.sql`. Tabelas:

- `hosts`: um por domínio. robots.txt, crawl_delay, max_pages, rank, blocked, lease.
- `frontier`: URLs descobertas e nunca buscadas. Chave é `url_hash`. A linha some quando vira página.
- `pages`: uma por URL buscada. Texto extraído inteiro, comprimido por TOAST lz4. `summary` nulo até
  alguém acessar a página. `out_links` guarda os `url_hash` apontados. `bm25` é gerado por trigger.
- `chunks`: fatias de `pages.text` por offset de caractere. `embedding rabitq8(512)`. `simhash` do
  texto embedado. Embedding nulo significa fila do embed worker.

Regras:

- `url_hash` é xxh3-64 da URL normalizada. Nunca indexar URL como texto.
- Normalização agressiva: minúsculas em host, sem fragmento, sem `utm_*` e afins, query ordenada,
  https quando possível, respeita `rel=canonical`.
- `content_hash` é SHA-256 do texto extraído, nunca do HTML. Texto igual em URLs diferentes
  não gera embedding novo.
- `pages.text` começa com o título na primeira linha. É o que o BM25 indexa.
- Intervalo adaptativo: conteúdo igual dobra `check_interval`, diferente divide por dois.
  Mínimo 1 dia, máximo 90 dias. Novo começa em 7 dias. HTTP 304 conta como igual.
- `fail_count` 10 tira a página do scheduler. Ela fica, só não é revisitada.
- Simhash por chunk. Só reembeda se a distância de Hamming passar de 3 bits. A coluna guarda o
  simhash do texto que foi embedado e só muda quando o embedding é refeito. Assim pequenas
  mudanças acumuladas não passam despercebidas.
- Chunk de cerca de 2048 tokens, medido em caracteres no `common`. 99% das páginas cabem em um.
- Profundidade máxima 5. Passou disso, quase sempre é erro ou spam.
- Não existe teto real por host. `max_pages` é válvula contra sites infinitos, padrão 1 milhão,
  e só se ajusta para baixo. Quem regula é a prioridade: ao devolver um lote, o host espera
  `sqrt(page_count)` segundos antes de voltar à fila, e recebe até `sqrt(page_count)` URLs por
  lote, no máximo 100. Sites pequenos e novos passam na frente, sites grandes andam devagar mas
  sempre andam. Frontier é servida do mais novo para o mais antigo.
- Sitemaps são relidos a cada 24 horas em todo host com conteúdo em português.
- O foco no Brasil é redução de escopo, não regra absoluta. Cada host tem `langs`, idiomas
  aceitos. Hosts `.br` nascem com `{pt,en}`, porque site brasileiro publicando em inglês é
  conteúdo nosso; os demais com `{pt}`, senão os links levam o crawler para a Wikipédia em
  inglês e o escopo some. Fontes de notícia e artigos sobre IA, que a IA do usuário vai buscar
  muito, entram com `{pt,en}` via `POST /fetch/seed` com `langs`. Regras baratas: hosts `.br`
  novos passam na frente dos outros novos; host com três páginas fora dos idiomas dele e nenhuma
  dentro é bloqueado e sua frontier apagada; sitemaps só são lidos a partir da primeira página
  aceita do host; redes sociais e afins são descartadas já na normalização da URL, lista em
  `common/urls.py`. `pt_count` conta páginas aceitas em qualquer idioma do host.
- `published_at` vem da data que o extrator acha na página. A busca dá um bônus por recência
  que vale até meia primeira posição e decai por e a cada 60 dias, constantes em `search.py`.
- Página com menos de 200 caracteres de texto extraído é um hub, tipo home de portal: fica sem
  texto e sem chunks, mas os links entram na frontier e o `content_hash` é da lista de links, então
  o intervalo adaptativo acompanha a home mudar. Idioma do hub vem do HTML inteiro.
- Índice vchordrq nasce com `lists = []`. Passou de um milhão de chunks, reindexar com `lists = [10000]`.

## Protocolo API e workers

Endpoints em `bingus/api/fetch.py`. Os modelos pydantic lá são a definição do protocolo.

- `POST /fetch/batch?hosts=10&per_host=10`: aluga hosts e devolve suas URLs, com `crawl_delay`
  e `robots` de cada host. Host sem `robots` é novo: o worker busca o robots.txt e devolve.
- `POST /fetch/results`: `pages` com o resultado de cada URL, `hosts` com robots e crawl_delay
  descobertos, `urls` achadas em sitemaps. Corpo JSON com gzip.
- `POST /fetch/seed`: lista de URLs para a frontier, profundidade 0. Marca os hosts como
  `seeded`, e a lease de hosts os serve antes de qualquer outro: sem isso, 90 mil hosts `.br`
  novos deixavam as fontes de IA semeadas sem nenhuma visita.
- `POST /metrics`: amostras de telemetria do worker, a cada ~10 s, mesma chave.
- `GET /health`.

Resultado de página: `status` 0 é erro de rede. `final_url` só quando houve redirect. `text` com o
título na primeira linha. `chunks` com offsets e simhash calculados no worker. `links` já
normalizadas. Quem não veio em português vira `foreign`: fica registrada sem texto e sem links,
revisitada em 90 dias. Só página em português alimenta a frontier.

- Autenticação por header `Authorization: Bearer <API_KEY>`. Uma chave por worker, com nome.
  `BINGUS_API_KEYS=nome:chave,outro:chave`.
- Trabalho em lotes grandes. O worker pega o lote, resolve tudo, devolve tudo de uma vez.
- Lease de fetch é por host, não por URL. A API nunca entrega o mesmo host a dois workers.
  Dentro do lote o worker respeita `crawl_delay` do host e roda hosts em paralelo.
- Prazo do lease é proporcional ao lote. Expirou, o host volta para a fila.
- O worker mantém `BINGUS_HOSTS` hosts em voo o tempo todo: cada host libera o slot ao
  terminar, e hosts novos são pedidos em lotes de um quarto do total. Resultados prontos vão
  juntos num `POST /fetch/results` a cada `BINGUS_FLUSH` segundos, padrão 15, porque um POST por
  host virava dezenas por
  segundo e uma linha em `batches` para cada. Antes o lote inteiro esperava o host mais lento e o
  worker fazia 2 páginas por segundo. Um host recebe no máximo 60 segundos de trabalho por lote.
  Throughput se ajusta em `BINGUS_HOSTS`, não em URLs por host; extração é uma thread só.
- Fetch worker busca `robots.txt` de host desconhecido, aplica localmente e devolve o conteúdo.
  A API guarda e usa para filtrar e dar ritmo. Sitemaps do robots viram URLs de profundidade 0.
- Resultado de fetch é JSON com gzip: status, headers relevantes, título, texto, idioma, links.
- Resultado de embed é msgpack com bytes crus. Sem base64.
- Prioridade do scheduler: hosts sem nenhuma página primeiro, depois páginas por `next_check_at`.
  Páginas muito ativas voltam sozinhas pelo intervalo curto.
- Só HTML estático. Sem JavaScript, sem PDF nesta fase.
- Extração roda numa thread só. lxml em várias threads ao mesmo tempo deu segfault no Windows.
  Rode workers com `PYTHONUNBUFFERED=1` quando redirecionar log para arquivo, senão um crash
  leva o buffer junto.

## Busca

Vive na API, em `bingus/api/search.py`. `GET /search?q=...&limit=10`, sem auth: é o que o
frontend e o MCP chamam. Híbrida:

- BM25 nas páginas, via `bm25 <&> to_bm25query(...)`, 200 candidatos. O índice só devolve
  até `bm25_catalog.bm25_limit`, padrão 100, então a consulta faz `SET LOCAL` para 200.
- Vetorial nos chunks, com a query embedada pelo embed worker em `BINGUS_EMBED_URL`, 200
  candidatos, melhor chunk por página. Embed worker fora do ar: cai para BM25 só, e a resposta
  avisa em `warnings`, lista de frases que o frontend mostra em alerta amarelo. BM25 falhando
  também entra ali; os dois juntos viram 503 com `detail`, que o frontend mostra em vermelho.
- Vetor da query em cache de RAM por 24 horas, chave é a query em minúsculas com espaços
  normalizados, até 10 mil entradas. Query repetida não toca a GPU.
- Paginação por `offset` sobre a lista fundida; `has_more` diz se há mais. O frontend faz
  scroll infinito de 20 em 20. Texto das páginas só é carregado para a fatia pedida.
- `timings` na resposta: milissegundos de bm25, embed_query, vector, fusion, pages, summaries e
  total. O frontend mostra na linha acima dos resultados.
- Fusão por Reciprocal Rank Fusion com k = 60 sobre três listas: BM25, vetorial e os mesmos
  candidatos ordenados por `pages.rank`. Sem pesos por enquanto.
- Resultados sem sumário ganham um na hora, em paralelo, com timeout de 20 s. Ver Sumários.
  `summaries=false` desliga a LLM: sumários já salvos ainda voltam, os que faltam ficam nulos.
  O frontend tem a chave "Gerar resumos", lembrada em localStorage.
- Snippet: janela de 300 caracteres do melhor chunk em volta da primeira palavra da query, ou o
  início da página. Resposta traz url, título, host, snippet, summary, score e a posição em cada
  lista, útil para depurar relevância.
- Rate limit por IP com token bucket em memória: rajada de 20, recarga de 1 por segundo, 429 ao
  estourar. Lê `X-Forwarded-For` porque o Caddy fica na frente. Constantes em `search.py`.
- CORS liberado para GET, porque o frontend chama a API direto do browser.

## Frontend

Vite puro em `frontend/`, congelado exceto `src/search.ts`, que chama `GET /search` na API em
`VITE_API_URL`, padrão `http://localhost:8000`. O dev server reescreve `/search` para
`search.html`; em produção o Dockerfile faz o build e um Caddy serve `dist` com a mesma regra.
`VITE_API_URL` entra como build arg pelo compose. Nesta máquina o npm não existe, use
`pnpm install` e `pnpm dev --host`. O `Caddyfile` da raiz é a borda pública com TLS.

## Sumários e página

`bingus/api/llm.py` fala com o OpenRouter, modelo `inclusionai/ling-3.0-flash`, chave em
`OPENROUTER_KEY`, endpoint em `OPENROUTER_BASE_URL`, padrão `https://openrouter.ai/api/v1`.
Sem chave, tudo devolve None e nada quebra. Só os primeiros 12 mil caracteres da página vão para
o modelo. Para rodar a API localmente com o `.env`: `set -a; . ../.env; set +a` antes do `uv run`,
sobrescrevendo `DATABASE_URL` para `localhost`.

- Sumário: gerado na primeira vez que a página aparece num resultado de busca ou em `/page`, um
  request por página em paralelo, persistido em `pages.summary`. A ingestão zera o sumário quando o
  conteúdo muda, e a gravação confere o `content_hash` para não salvar sumário de texto velho.
- `GET /page?url=...&prompt=...`: página guardada, com texto completo. Com prompt, a LLM responde
  sobre o texto e o resultado fica só em RAM, chave `(content_hash, prompt)`, TTL de 12 horas,
  mil entradas. URL desconhecida devolve 404 e entra na frontier. Buscar na hora fica para o MCP.

## PageRank

`bingus-rank`, em `bingus/api/rank.py`. Processo à parte, cron diário: `docker compose exec api
bingus-rank`. Carrega páginas e links do Postgres por `COPY` direto em numpy, resolve os hashes de
destino por busca binária, monta matriz esparsa em scipy e roda 20 iterações com amortecimento
0.85 e tratamento de páginas sem links. Grava `pages.rank` e `hosts.rank` com média 1. Páginas de
redirect guardam o destino em `out_links` para repassar rank. Em 11 mil páginas leva 6 s; em 10
milhões estima-se minutos e uns 12 GB de RAM no pico.

Regra aprendida a caro: nunca um UPDATE só sobre a tabela `pages` inteira. Com 1,17 milhão de
linhas ele levou 45 minutos segurando o crawler travado e morreu em deadlock com o fetch worker.
Rank e seed gravam em lotes de 50 mil ids, cada um em transação própria.

## Seeds

`POST /fetch/seed` recebe uma lista de URLs para a frontier. Para a Wikipédia existe
`bingus-seed wikipedia data/ptwiki.tar.gz [pular N]`, em `bingus/api/seed.py`:

- Dump HTML do Wikimedia Enterprise, `ptwiki-NS0-<data>-ENTERPRISE-HTML.json.tar.gz` em
  `dumps.wikimedia.org/other/enterprise_html/runs/`. Uns 16 GB, 1,4 milhão de artigos. Lido em
  streaming, nunca extraído no disco. `data/` está no gitignore.
- Extração em processos filhos, `BINGUS_SEED_WORKERS`, padrão 4. No Windows cada processo
  importa tudo de novo, 16 processos estouraram a memória.
- Cada artigo passa por `ingest`, a mesma função dos resultados do crawler: chunks, twins,
  `out_links`, tudo igual. Idioma forçado para `pt`, porque o detector erra em artigo curto.
  Redirects e vazios são pulados. Sufixo "– Wikipédia, a enciclopédia livre" removido.
- Deadlock com o fetch worker acontece e é esperado: o lote é repetido até cinco vezes.
- Links internos não vão para a frontier, o dump já traz os artigos. Links externos só entram se
  o host termina em `.br`. Tudo ainda vai para `out_links`, então o PageRank vê o grafo inteiro.
- No fim, revisitas espalhadas ao acaso por 90 dias, `max_pages` do host em 2 milhões.
- Reexecutar é seguro: artigo igual cai no caminho "unchanged". O terceiro argumento pula as
  primeiras N linhas para retomar uma carga interrompida.
- Extração do lote seguinte roda enquanto o atual é gravado. Uns 40 artigos por segundo com 8
  processos; o gargalo é a gravação sequencial no banco. O dump inteiro leva umas 10 horas.
- Progresso em `seed_progress`, criada pelo próprio seed, atualizada a cada lote com bytes lidos
  do dump. `GET /seed/status` devolve isso mais os últimos 20 artigos; `frontend/seed.html`
  mostra com barra, ETA e tabela, atualizando a cada 5 s.
CommonCrawl filtrado por Brasil entra da mesma forma no futuro.

## Métricas

Sem Prometheus. Tudo vai para o Postgres e o Grafana lê com a datasource Postgres, usando um
usuário só de leitura. Três tabelas, todas com retenção de 90 dias, limpas pela própria API:

- `worker_samples`: o worker faz push a cada ~10 s em `POST /metrics` com a mesma chave.
  Vai junto `instance`, o hostname ou `BINGUS_NAME`, e a API grava `worker` como `fetch/nome`:
  várias máquinas com a mesma chave viram séries separadas.
  Cada amostra traz requests, bytes baixados, páginas prontas, erros por tipo e CPU do processo
  no período. Workers não abrem porta nenhuma.
- `batches`: a API grava uma linha por resultado ingerido, com contagem por desfecho, URLs
  novas na frontier, chunks enfileirados e tempo de ingestão. É daqui que sai taxa de sucesso.
- `api_samples`: estado geral a cada minuto, com estimativas do planner para as tabelas grandes.

Rankings como hosts com mais páginas ou páginas com maior rank são consultas diretas nas
tabelas principais. Capriche no dashboard: taxa por worker, taxa de sucesso, bytes por segundo.

O dashboard está em `grafana/bingus.json`, para importar no Grafana com a datasource Postgres
escolhida na variável `DS`. Buckets acompanham o zoom com mínimo de 5 minutos; taxas dividem a
soma pelo `$__interval_ms`. Dois índices existem só para ele: `chunks_embedded_at`, com
`chunks.embedded_at` gravado ao salvar o vetor, e `pages_foreign`, parcial sobre as páginas
rejeitadas por idioma, que ficam com `lang` e sem `content_hash`.
Ao mexer, valide cada query no psql trocando `$__timeGroupAlias` por `date_trunc` e
`$__timeFilter` por um filtro de data.

## Deploy no dedicado

Imagens no Docker Hub: `shiftsad/bingus:api`, `:fetch`, `:embed`. O compose usa as imagens; o
perfil `workers` sobe fetch e embed na mesma máquina, o embed com a GPU via toolkit da NVIDIA.

`.env` de produção: `POSTGRES_*`, `DATABASE_URL` com `postgres` como host, `BINGUS_API_KEYS` com
uma chave por worker, `BINGUS_FETCH_KEY` e `BINGUS_EMBED_KEY` para os workers do compose,
`BINGUS_EMBED_URL=http://embed:8100`, `OPENROUTER_KEY`, `VITE_API_URL`, e os binds no host:
`BINGUS_API_BIND=10.10.0.2:2026` e `BINGUS_WEB_BIND=10.10.0.2:2027`, o IP da WireGuard do dedicado.
Sem essas duas, o padrão é loopback nas portas 8000 e 8080. Nada do Bingus escuta na interface
pública; quem fala com a internet é o Caddy do VPS, pela WireGuard. Como o Docker faz bind no IP
da wg0, ela precisa estar de pé antes do compose subir no boot.

Migrar o banco do PC para o dedicado, nesta ordem:

1. Parar os workers e a API no PC. Nada mais escreve.
2. `docker compose exec -T postgres pg_dump -U bingus -Fc bingus > bingus.dump`. Custom format,
   comprimido, uns 40% do tamanho do banco. Os índices são recriados no restore.
3. Copiar o dump para o dedicado. No dedicado, `docker compose up -d postgres` cria o banco vazio
   com o `schema.sql`; depois `docker compose cp bingus.dump postgres:/tmp/bingus.dump` e
   `docker compose exec postgres pg_restore -U bingus -d bingus --clean --if-exists --no-owner -j 4 /tmp/bingus.dump`.
   Restore paralelo não aceita stdin, por isso a cópia. As extensões já existem, os avisos sobre
   elas são normais. Apague o `/tmp/bingus.dump` do container no fim.
4. `docker compose up -d api frontend` e, se a GPU estiver configurada,
   `docker compose --profile workers up -d`.
5. No PC, workers apontando para a API do dedicado pela WireGuard ou pelo Caddy.
6. Cron diário: `docker compose exec api bingus-rank` e
   `docker compose exec postgres psql -U bingus -c "REINDEX INDEX CONCURRENTLY pages_bm25"`.
   Um `VACUUM ANALYZE pages` de vez em quando também.

O Postgres publica em `BINGUS_PG_BIND`, padrão `127.0.0.1:5432`; no dedicado é o IP da WireGuard
numa porta livre, para o Grafana e o psql do PC. Nunca na interface pública. O embed só expõe
8100 dentro da rede do compose.

Grafana: usuário só de leitura, `CREATE ROLE grafana LOGIN PASSWORD '...'; GRANT SELECT ON ALL
TABLES IN SCHEMA public TO grafana;`, datasource Postgres.

## Futuro, não fazer agora

CommonCrawl, notícias, RSS, PDF, o MCP em si, reescrita da busca, sumários.

## Verificado no container

- Postgres 18 guarda dados em `/var/lib/postgresql`, sem `/data`.
- `BINGUS_EMBED_LEASE=0` deixa o embed worker só respondendo queries em `/embed`, sem puxar
  lote. É o modo do dedicado: na 1050 Ti uma passada de lote leva 4 s e a query esperava atrás.
- torch 2.14 em Linux roteia até `matmul` para kernels Triton (`torch._native`), que exigem gcc
  na imagem e não rodam em Pascal. O Dockerfile fixa `TORCH_DISABLE_NATIVE_JIT=1`; no Windows
  nativo não há Triton e o problema não aparece.
- `shared_preload_libraries=vchord.so,pg_tokenizer.so,vchord_bm25.so` é obrigatório.
- Funções do tokenizador vivem em `tokenizer_catalog` e precisam do schema qualificado no initdb.
- O índice bm25 gasta uma página de 8 KB por termo do vocabulário. Modelo customizado, que
  aprende termo novo a cada palavra nunca vista, chegou a 1,5 milhão de termos e 12 GB de índice
  com 335 mil artigos da Wikipédia, para 1,2 GB de dados. Por isso o tokenizador é o `gemma2b`
  pré-treinado, 256 mil tokens fixos, com filtro de minúsculas. Sem stemmer: subpalavras fazem
  parte do trabalho. No TOML, `model` vem antes dos blocos `[[character_filters]]`.
- A coluna `bm25` é preenchida por um trigger plpgsql simples em INSERT e UPDATE de `text`.
- O índice bm25 incha com inserção incremental: 14 GB para 1,17 milhão de páginas, que caem para
  2,6 GB depois de `REINDEX INDEX CONCURRENTLY pages_bm25`, uns 3 minutos e sem travar nada.
  Reindexar de tempos em tempos, junto com o PageRank diário, por exemplo. Compacto, custa uns
  2,2 KB por página.
- `rabitq8(512)` ocupa 536 bytes por linha. `halfvec(512)` ocupa 1032. `vector(512)` ocupa 2056.
- `rabitq8` não aceita `residual_quantization` no índice.
- `rabitq8 <=> rabitq8` devolve o cosseno negativo, não a distância: vetor consigo mesmo dá -1.
  Ordenar crescente funciona igual. Para distância real use `1 + (a <=> b)`.
- Busca BM25: `bm25 <&> to_bm25query('pages_bm25', tokenizer_catalog.tokenize(q, 'pt'))`,
  ordenar crescente, score é negativo.
