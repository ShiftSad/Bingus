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
- O backend NestJS foi apagado. O frontend é Vite puro, ver Frontend.
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

- Desde 2026-11-19 tudo roda numa máquina Linux compartilhada (`craftsapiens`, 10.10.0.50 na
  WireGuard) com a 3060 Ti inteira, Docker rootless. A máquina roda outras coisas (Pterodactyl, MySQL
  em `/mnt/hdd`, outro usuário): **nunca reiniciar** e nunca passar do orçamento.
- Orçamento: 32 GB de RAM somando tudo, 16 CPUs, 100 GB de NVMe, até 1 TB no HDD (`/mnt/hdd/bingus`,
  SMR, IOPS da conta limitados a 100 de leitura e 60 de escrita, dividido com o MySQL). O teto de RAM e
  CPU é o `bingus.slice` do systemd do usuário, ver Deploy.
- Banco no NVMe, menos as tabelas `texts` e `links`, no tablespace `hdd`. O HDD fica para isso e
  para arquivos grandes e sequenciais: dumps, backups.
- O dedicado antigo (10.10.0.2, Postgres com 1,17 milhão de páginas) e `api.bingus.sh` estavam fora do
  ar nessa data. O banco daqui nasceu vazio.
- VPS de entrada com Caddy e TLS na frente da API (10.10.0.1 na WireGuard); aponta para o dedicado antigo.
- Alvo: 10 milhões de páginas em cerca de 50 GB. Guia, não teto.

## Modelo de embedding

`perplexity-ai/pplx-embed-v1-0.6b`. 1024 dims, Matryoshka, saída int8 sem normalização,
similaridade por cosseno, sem prefixo de instrução, contexto 32K.

- Truncar para 512 dims. Enviar int8 cru, 512 bytes por vetor.
- fp16 dá NaN neste modelo. bf16 em Ampere ou mais nova, fp32 na 1050 Ti, escolhido sozinho.
  A saída do modelo já é int8, entregue como float32 com valores inteiros.
- 512 tokens por chunk de 1700 caracteres (`CHUNK_CHARS` em `common/text.py`), batch 8.
  `BINGUS_EMBED_TOKENS` e `BINGUS_EMBED_BATCH`. Medido em 2765 páginas de produção e 630 queries
  (scratchpad do agente de 2026-11-19): chunk de 1700 com 512 tokens ganha do de 7000 com 2048 em
  trecho do meio/fim da página (MRR@10 0,732 contra 0,589, R@10 0,896 contra 0,754), empata em
  título e embeda 26% mais páginas por segundo; custa uns 3,2 chunks por página em vez de 1,3.
  Truncar o chunk de 7000 em 512 tokens perdia o que vem depois do começo (MRR 0,500). Na troca,
  os chunks pendentes de 7000 foram refeitos; os já embedados ficaram como estavam.
  `BINGUS_EMBED_LEASE` é quantos chunks pega da API por vez; 256 mantém a GPU sem vales.
- O worker ordena o lote alugado por tamanho antes de dividir em passadas: cada passada é preenchida
  até o maior texto dela, e em ordem aleatória quase toda passada pagava 2048 tokens. Medido na 3060
  Ti com 256 chunks da Wikipédia (média 2637 caracteres), batch 8: 2048 tokens, 7,3 chunks/s fora de
  ordem e 16,2 ordenado; 1024 tokens, 15,3 e 25,5; 512 tokens, 32,4 e 42,7. Batch 32 dá no mesmo.
  Pior passada com 2048 tokens: 1,3 s, que é o máximo que uma query de busca espera.
- Query e documento usam o mesmo modelo sem prefixo.
- O embed worker serve `POST /embed` na porta `BINGUS_EMBED_PORT`, padrão 8100, para queries de
  busca. Sem auth, só na rede interna. Query e lote se revezam num lock: a query espera no
  máximo um mini-batch. Corpo `{"texts": [...]}`, resposta int8 em listas.
- Torch vem do índice CUDA 12.6 do PyTorch, o último que roda na 1050 Ti. Configurado no
  `pyproject.toml`. No Windows a instalação é a mesma, `uv sync --extra embed`.
- Fluxo: `POST /embed/batch?size=64` aluga chunks por 15 minutos e devolve o texto de cada um.
  A fila é o índice parcial `chunks_pending` sobre `chunks.priority`, gravada ao enfileirar com a
  função SQL `embed_priority(host, rank)`: o PageRank da página, com a Wikipédia entre -1 e 0, atrás
  de qualquer outro host. O `bingus-rank` refaz a prioridade dos pendentes. A versão anterior
  percorria `pages_rank` passando por todas as páginas já embedadas e ficava mais lenta a cada dia.
  `POST /embed/results` recebe msgpack com bytes crus e grava com `quantize_to_rabitq8`.
- Texto igual em várias páginas é embedado uma vez só: a lease entrega o chunk só à página de menor
  id com aquele `content_hash`, e o resultado é copiado para as gêmeas pendentes (sem `embedded_at`,
  que conta só o trabalho da GPU). Se a cópia ficou pendente com a gêmea já pronta, ela volta à fila.

## Banco

Imagem `tensorchord/vchord-suite:pg18-latest`. Extensões `vchord`, `pg_tokenizer`, `vchord_bm25`.
Schema em `bingus/schema.sql`. Tabelas:

- `hosts`: um por domínio. robots.txt, crawl_delay, max_pages, rank, blocked, lease.
- `frontier`: URLs descobertas e nunca buscadas. Chave é `url_hash`. A linha some quando vira página.
- `pages`: uma por URL buscada. `summary` nulo até alguém acessar a página. `bm25` é gravado pela
  ingestão, no mesmo UPDATE, a partir do texto.
- `texts`: texto extraído inteiro de cada página com texto, chave `page_id`, no HD. Ver Deploy.
- `links`: `targets` guarda os `url_hash` apontados pela página, até 500, chave `page_id`, no HD.
  Página sem links não tem linha. Só o PageRank lê.
- `chunks`: fatias de `texts.text` por offset de caractere. `embedding rabitq8(512)`. `simhash` do
  texto embedado. Embedding nulo significa fila do embed worker, na ordem de `priority`.
- `pages_content` é `(content_hash, id)`: acha a gêmea de menor id sem ordenar todas as cópias.

Regras:

- `url_hash` é xxh3-64 da URL normalizada. Nunca indexar URL como texto.
- Normalização agressiva: minúsculas em host, sem fragmento, sem `utm_*` e afins, query ordenada,
  https quando possível, respeita `rel=canonical`.
- `content_hash` é SHA-256 do texto extraído, nunca do HTML. Texto igual em URLs diferentes
  não gera embedding novo.
- `texts.text` começa com o título na primeira linha. É o que o BM25 indexa.
- Intervalo adaptativo: conteúdo igual dobra `check_interval`, diferente divide por dois.
  Mínimo 1 dia, máximo 90 dias. Novo começa em 7 dias. HTTP 304 conta como igual.
- `fail_count` 10 tira a página do scheduler. Ela fica, só não é revisitada.
- Simhash por chunk. Só reembeda se a distância de Hamming passar de 3 bits. A coluna guarda o
  simhash do texto que foi embedado e só muda quando o embedding é refeito. Assim pequenas
  mudanças acumuladas não passam despercebidas.
- Chunk de 1700 caracteres, cerca de 512 tokens, cortado em parágrafo ou frase no `common`.
- Profundidade máxima 3, contada a partir da semente ou do sitemap (`MAX_DEPTH`). Era 5: em 20/11,
  63% das 450 mil páginas de 6 horas estavam a 4 ou 5 cliques da entrada, previsão do tempo de
  cada município, listagem de produto, reclamação. 1,5 milhão de URLs da frontier com mais de 3
  saltos foram apagadas.
- Não existe teto real por host. `max_pages` é válvula contra sites infinitos, padrão 1 milhão,
  e só se ajusta para baixo. Quem regula é a prioridade: ao devolver um lote, o host espera
  `sqrt(page_count)` segundos antes de voltar à fila, e recebe até `sqrt(page_count)` URLs por
  lote, no máximo 100. Sites pequenos e novos passam na frente, sites grandes andam devagar mas
  sempre andam. Frontier é servida do mais raso para o mais fundo e, na mesma profundidade, do mais
  novo para o mais antigo (índice `frontier_host`): sementes e sitemaps antes dos links achados no
  caminho. Do mais novo primeiro, o link fundo recém-achado passava na frente de tudo.
- Sitemaps são relidos a cada 24 horas em todo host com conteúdo em português.
- O foco no Brasil é redução de escopo, não regra absoluta. Cada host tem `langs`, idiomas
  aceitos. Hosts `.br` nascem com `{pt,en}`, porque site brasileiro publicando em inglês é
  conteúdo nosso; os demais com `{pt}`, senão os links levam o crawler para a Wikipédia em
  inglês e o escopo some. Fontes de notícia e artigos sobre IA, que a IA do usuário vai buscar
  muito, entram com `{pt,en}` via `POST /fetch/seed` com `langs`. Regras baratas: hosts `.br`
  novos passam na frente dos outros novos; host com três páginas fora dos idiomas dele e nenhuma
  dentro é bloqueado e sua frontier apagada; sitemaps só são lidos a partir da primeira página
  aceita do host; redes sociais e afins são descartadas já na normalização da URL, lista em
  `common/urls.py`, e também host que não é domínio (`VALID_HOST`: sem ponto, com espaço, `%`,
  telefone; eram 0,9% dos hosts, nenhum com texto). `pt_count` conta páginas aceitas em qualquer
  idioma do host.
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
- `GET /metrics`: formato do Prometheus, com chave, para o Alloy. Ver Métricas.
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
- Cada host tem no máximo 60 s de relógio por lote (`HOST_BUDGET`, robots e sitemaps inclusos): o
  worker para ao estourar e o resto fica na fila. A API dimensionava o lote só pelo `crawl_delay`,
  sem o tempo de download, e um host segurava o slot por 2 a 4 minutos.
- O worker espera o `crawl_delay` antes de cada request (`Pace`), nunca além do orçamento: se a
  espera não cabe, devolve o host. Ao devolver, a API só realuga depois do `crawl_delay`
  (`COOLDOWN`). Crawl-delay sem teto existe: 175 hosts do foco pediam mais de 60 s, alguns 86400.
  Antes o worker dormia o intervalo inteiro depois da página, a lease vencia, o mesmo host voltava
  num lote novo e prendia outra vaga. Em 20/11 as vagas presas passaram de 75, o worker parou de
  pedir hosts (pede só com um quarto livre) e a coleta ficou 4 horas em zero sem nenhum aviso no
  log. `flying` é um conjunto de nomes, então o desligamento mostrava "1 hosts released".
  Com intervalo de um lote ou mais, o host não lê sitemaps: eles tomariam a vez das páginas.
- Lease fixa de 3 minutos (`LEASE_SECONDS`): orçamento, o download em curso e o próximo envio.
  Expirou, o host volta para a fila. Antes crescia com o `crawl_delay` e chegava a 50 minutos.
- O worker mantém `BINGUS_HOSTS` hosts em voo o tempo todo: cada host libera o slot ao
  terminar, e hosts novos são pedidos em lotes de um quarto do total. Resultados prontos vão
  juntos num `POST /fetch/results` a cada `BINGUS_FLUSH` segundos, padrão 15, porque um POST por
  host virava dezenas por
  segundo e uma linha em `batches` para cada. Antes o lote inteiro esperava o host mais lento e o
  worker fazia 2 páginas por segundo. Um host recebe no máximo 60 segundos de trabalho por lote.
  Throughput se ajusta em `BINGUS_HOSTS`, não em URLs por host; extração é uma thread só.
- Um worker satura um núcleo (GIL: HTTP, extração e links no mesmo processo), então se escala em
  processos. Nesta máquina roda 1 worker nativo com `BINGUS_HOSTS=64` (`~/.config/bingus/fetch.env`):
  o bastante para enfileirar chunks no ritmo que a GPU vetoriza, ~40 por segundo. Com 12 a coleta
  enfileirava 236 chunks/s, ocupava 71% da CPU da máquina e a fila do embed só crescia. Nos hosts
  do foco, portais grandes que respondem devagar, o worker espera rede: com 40 em voo fazia 5
  páginas/s a 0,17 núcleo; com 120, 28 páginas/s e 50 chunks/s a 1,1 núcleo. Depois do `Pace`, os hosts
  devolvem a vaga mais cedo: com 100, 30 páginas/s e 50 chunks/s, daí 80. Com a frontier rasa (20/11)
  vêm mais artigos inteiros: 80 davam 21 páginas/s e 54 chunks/s, daí 64. Desde 19/11 a coleta é focada:
  `BINGUS_MIN_PRIORITY=4` faz a lease entregar só hosts com prioridade 4 ou mais, ou semeados à mão
  (`fetch.py`, `MIN_PRIORITY`). O mesmo filtro vale para a frontier: link para host abaixo do corte
  não entra (`add_frontier`, `min_priority`; seed e `/page` passam 0), senão acumulava URL que
  nunca seria buscada, 5,6 milhões apagados em 19/11. Medido com o filtro: 99,8% das páginas de hosts com prioridade 6,
  ~210 páginas/s e falha de 14%. Ver Referências. Antes, com 12 e 60 hosts em voo, uns 20 páginas novas por segundo
  no total. Extração custava 258 ms de CPU por página, 85% no `bare_extraction`; com `fast=True` do
  trafilatura caiu 2,4x, artigos idênticos, e homes de portal viram hubs.
- Nesta máquina os fetch workers rodam fora do Docker (`systemd/bingus-fetch@.service`, venv em
  `~/bingus-data/fetch-venv`, env em `~/.config/bingus/fetch.env`): a rede do Docker rootless
  (gvisor-tap-vsock) entregava uns 5 MB/s somando todos os containers, contra 60 MB/s do host. O
  venv é instalado sem editable; mudou o código do fetch, rode o `uv sync` do cabeçalho de
  `systemd/bingus.slice` e reinicie as instâncias. As réplicas de fetch do compose ficam em 0.
- robots.txt é interpretado com `protego`, pela RFC 9309: vale a regra mais específica. O
  `RobotFileParser` do Python usa a primeira que casa, e um `Allow: /` antes dos Disallow (comum,
  é o caso do radargpu) liberava o site inteiro. A URL também é testada com barra no fim do caminho,
  porque a normalização a tira e `Disallow: /x/` não pegava `/x?a=1`.
- 429 ou 503 numa página: o worker dobra o `crawl_delay` do host (de 5 a 60 s), grava pelo
  resultado do host e deixa o resto do lote na fila.
- Extração: `<form>` é desembrulhado (ASP.NET e fóruns Invision põem a página inteira num, e o
  trafilatura apaga form com conteúdo), `<article>` com irmão `<article>` vira `div` (senão só o
  primeiro card sobrevivia), página com `DiscussionForumPosting` sai sem `favor_precision` (que
  apaga posts de fórum) e href quebrado é descartado em vez de derrubar a página.
- robots.txt e sitemaps baixam com teto de 10 MB (inclusive depois de descompactar) e 20 s por
  arquivo, e a lista de URLs é cortada antes de normalizar. Sem isso, sitemaps de portal com
  centenas de MB levavam minutos, a lease vencia no meio, outro worker pegava o host e os maiores
  sites semeados nunca passavam da primeira página.
- A data de publicação vem do htmldate sem busca extensiva (`extensive_search=False`,
  `original_date=True`): 37 ms contra 142 ms por página, e em 13 páginas só uma home perdeu a data.
- A ingestão de `/fetch/results` usa uma transação curta por host (`in_tx`, que repete só aquele
  pedaço em deadlock), outra para a frontier, e grava frontier e hosts em ordem; o `next_due` de um
  host só é escrito quando muda. Uma transação por lote segurava por segundos as linhas de hosts
  que toda página linka (gov.br e afins): com 12 workers dava deadlock cinco vezes seguidas, o
  worker guardava o lote e reenviava maior, e os hosts do lote nunca eram liberados.
- `docker stop` (SIGTERM) faz o worker enviar o que está pronto e devolver os hosts em voo em
  `POST /fetch/release`, com `stop_grace_period` de 30 s. A devolução vem antes do envio, para um
  envio que falha não impedi-la. Sem isso, cada deploy deixava centenas de hosts alugados e o worker
  novo ficava em "no work". No Windows não há sinal: a lease vence sozinha em 3 minutos.
- Envio que falha volta para a fila do worker e vai no próximo flush, até 1000 hosts guardados.
  A API repete o lote inteiro até cinco vezes em deadlock e grava hosts e páginas em ordem de host,
  o que reduz os deadlocks entre workers e o seed.
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
  candidatos ordenados por `pages.rank`. O PageRank pesa metade (`RANK_WEIGHT`): com peso cheio, ser o
  primeiro em autoridade valia o mesmo que ser o primeiro em relevância.
- Ajustes depois da fusão, medidos num A/B às cegas com 40 buscas nerd (nDCG@10 0,471 -> 0,691,
  top1 bom 0,28 -> 0,60): bônus de até uma primeira posição pelas palavras da busca no título ou
  na URL, sem acento e com números inteiros (o tokenizador do BM25 quebra "5060" em dígitos),
  cada termo pesado pela raridade no corpus (amostra de ~27 mil títulos, renovada de hora em hora,
  em segundo plano). Raridade entre os candidatos invertia a intenção: o termo que trouxe os
  candidatos aparece em quase todos. Medido às cegas: pesos iguais 0,619, raridade entre
  candidatos 0,623, raridade no corpus 0,637, fusão sem ajustes 0,430. Recência só quando a busca pede (notícia,
  hoje, lançamento, novo, ano); metade da nota para tag, perfil, autor, categoria e busca interna;
  busca curta que é o nome de um site põe a raiz dele em primeiro, mesmo sendo hub.
  `boosts=false` desliga os ajustes, para comparar. Vetorial busca 400 chunks (uns 3 por página).
- Diversidade: um site ocupa no máximo `MAX_PER_HOST`, 2, posições antes de os outros aparecerem; o
  excesso vai para o fim da lista, não some. Com a Wikipédia inteira no índice a busca virava uma busca
  da Wikipédia, e não é essa a graça.
- Resultados sem sumário ganham um na hora, em paralelo, com timeout de 20 s. Ver Sumários.
  `summaries=false` desliga a LLM: sumários já salvos ainda voltam, os que faltam ficam nulos.
  O frontend tem a chave "Gerar resumos", lembrada em localStorage.
- Correção de acentos antes do BM25, à moda do "Mostrando resultados para": o tokenizador Gemma não
  tem nada em comum entre "memoria" e "memória", e sem acento a busca trazia lixo. Cada palavra sem
  acento vira a grafia acentuada quando o corpus a escreve assim 2,5 vezes mais (amostra de títulos
  e textos, renovada de hora em hora); stopwords, `NO_ACCENT` (pais, esta, pe) e busca com palavra
  inglesa ficam como vieram. A resposta traz `corrected`; o MCP mostra "Buscado como", o SearXNG
  põe em `corrections`, o frontend mostra o aviso. Só o BM25 e o snippet usam a correção; vetor e
  bônus de título já ignoram acento. Medido em 14 pares com e sem acento: sobreposição com a busca
  acentuada 20 -> 134 de 140. Acrescentar a grafia em vez de trocar não melhorou nada (25 -> 25).
  Reindexar tudo sem acento resolveria também página escrita sem acento, mas custa retokenizar o
  índice inteiro; ficou de fora.
- Snippet: janela de 300 caracteres do melhor chunk em volta da primeira palavra da query, ou o
  início da página. Resposta traz url, título, host, snippet, summary, score e a posição em cada
  lista, útil para depurar relevância.
- Rate limit por IP com token bucket em memória: rajada de 20, recarga de 1 por segundo, 429 ao
  estourar. Lê `X-Forwarded-For` porque o Caddy fica na frente. Constantes em `search.py`.
- CORS liberado para GET, porque o frontend chama a API direto do browser.

## Foco nerd/tech

Desde 2026-11-19 o foco é conteúdo nerd/tech: IA (português; inglês aceito para fontes como
Hugging Face), hardware e peças de computador, dev, Linux, games, ciência. Conteúdo geral continua,
com menos prioridade. O MCP é para um servidor de amigos nerds: o nicho bom primeiro.

- `hosts.priority` ordena a lease de hosts (antes de `seeded`) e a fila do embed
  (`embed_priority` soma 100 por ponto, lido da tabela `hosts`, por isso a função é STABLE).
  O cooldown entre lotes é dividido por `1 + priority`.
- `POST /fetch/seed` aceita `priority`. Os sites do dono têm 10: radardehardware.com.br, que
  redireciona para radargpu.com.br, e os irmãos radarcpu, radarram, radarautos (HTML estático,
  robots libera tudo menos `/comparar/livre/`, sitemap em `/sitemap-index.xml`). Também
  vrosa.com.br e vozport.com, com `langs` {pt,en} porque o vozport tem versão em inglês em `/en/`.
- Crawl focado simples: um host ainda nunca visitado, linkado de um host com prioridade p, nasce
  com p - 2 (`PRIORITY_DECAY`). Só nesses o insert usa DO UPDATE, que trava a linha; para os demais,
  DO NOTHING, porque hosts populares eram fonte de deadlock.

## Referências (Tavily e Exa)

Desde 19/11 a meta é cobertura, não escala: o que as buscas de referência trazem precisa estar no
índice. Chaves no `.env`: `TAVILY_API_TOKEN` (1 crédito por busca básica, 100 por minuto) e
`EXA_API_TOKEN` (US$ 0,007 por busca `auto` de até 10 resultados, 10 por segundo). O `country` do
Tavily piorou os resultados; o Exa vai com `userLocation: "BR"`.

- Medido em 80 buscas (40 nerd da avaliação, 40 gerais do dia a dia): dos 10 primeiros de cada
  referência, o Bingus tinha indexados 9 a 12% nas nerd e 4% nas gerais. A maior parte faltando era
  de sites que o crawler já conhece (34 a 50%), como gov.br, g1, techtudo, clubedohardware: o crawl
  espalhado não pega as páginas que as pessoas procuram. 9 a 11% das nerd eram sites em inglês
  bloqueados pela regra de idioma; a Wikipédia tinha 15 páginas, sem o dump carregado.
- As 1.846 URLs citadas foram semeadas com prioridade 6 e `langs` de cada host intocado. Seis
  documentações em inglês citadas mais de uma vez foram desbloqueadas com `{pt,en}`: docs.docker.com,
  docs.gitlab.com, ubuntu.com, freecodecamp, geeksforgeeks, cloud.google.com.
- Uso real: o log de um bot de Discord de um amigo, na mesma bolha tech (`~/bingus-data/api`),
  tem 209 buscas no Tavily de 8 a 18/11, metade em inglês: notícias do dia ("notícias boas" de
  ciência), preço e lançamento de modelos de IA, hardware, hospedagem e e-mail, eletrônica DIY.
  Das 1.148 URLs que ele recebeu, o Bingus tinha 3%; 15% eram de sites em inglês bloqueados pela
  regra de idioma. Os 552 sites citados passaram a `{pt,en}`, desbloqueados, prioridade 6 com duas
  citações ou mais e 4 com uma; 840 URLs semeadas. Com o ok do amigo, as mesmas buscas no Exa
  (US$ 1,46) trouxeram 1.796 URLs a mais, quase sem redes sociais: mais 1.047 sites pela mesma regra,
  313 com prioridade 6 somando as citações dos dois, 1.506 URLs novas na fila. Fora: reddit (robots barra), en.wikipedia,
  finance.yahoo (republica agências) e rocketreach (dados de pessoas). Do log só se usa a parte do
  Tavily; as conversas com o LLM e os dados do Discord ficam intocados.
- Em 20/11 a lease mostrou que prioridade abaixo de 6 é fome: os ~770 hosts com 6 sempre tinham
  trabalho vencido, e os 933 semeados com 4 (citados uma vez) e 92 com 0 (gerais bons, como STF,
  Embrapa, Dicio) não recebiam visita nenhuma. Todo host semeado foi para 6. Fora do foco à mão:
  reddit (robots barra), Wikipédia que não é a pt, Yahoo Finance, RocketReach, tavily.com (o Tavily
  devolve páginas dele mesmo), tracxn e cbinsights (paywall), com prioridade 0 e sem `seeded`.
- Rodada de 20/11: 398 buscas novas (`~/bingus-data/referencias/consultas.txt`, por seção: IA,
  hardware, dev, Linux, redes, celulares, games, ciência, maker, segurança, dia a dia, notícias),
  no Exa (US$ 2,77) e no Tavily (396 créditos de 1000 do mês), resultados em
  `/mnt/hdd/bingus/referencias-2026-11-20.jsonl`. Antes de semear, o Bingus tinha 14% das 7.045
  URLs citadas (Exa 15%, Tavily 12%; dia a dia 22%, ciência 8%); 47% eram de hosts conhecidos sem
  aquela página. 5.016 URLs semeadas com prioridade 6 e `{pt,en}`; 109 hosts bloqueados pela regra
  de idioma e citados duas vezes ou mais foram desbloqueados (tomshardware, realpython, docker,
  redis, postgresql, caddy, tailscale, nextjs...) com `max_pages` 2000: o site em inglês entra com
  as páginas citadas e a vizinhança rasa, não inteiro; chegando a 2000, a frontier recusa até
  semente, então numa rodada futura suba o `max_pages` deles antes de semear. Lojas, finanças e sites enormes ficaram
  bloqueados. Os scripts estão em `~/bingus-data/referencias`: `referencias.py` busca,
  `cobertura.py` e `cobertura.sh` medem e listam o que falta, `semear.py` semeia.

## API pública

Ainda sem IP público: tudo pronto para quando o dono quiser liberar pelo Caddy do VPS
(`Caddyfile` da raiz já aponta para 10.10.0.50).

- `GET /search?q=...&format=json&pageno=N` responde no formato JSON do SearXNG (`results` com `url`,
  `title`, `content`, `engine`), que Open WebUI, LangChain e afins já consomem: basta apontar para o
  Bingus como se fosse um SearXNG.
- `BINGUS_SEARCH_KEYS=chave1,chave2` fecha `/search`, `/page` e `/mcp` com `Authorization: Bearer`
  ou `?key=`. Vazio, aberto: o padrão enquanto só a WireGuard alcança.
- O host público precisa estar em `BINGUS_MCP_HOSTS`, senão o MCP responde 421.

## MCP

O produto. `bingus/api/tools.py`, SDK oficial `mcp` 2.x (`MCPServer`), montado na própria API em
`/mcp` com transporte HTTP streamable. Duas ferramentas:

- `search(query, limit)`: chama `run_search`, o núcleo de `/search` sem o rate limit por IP, sem LLM.
  Devolve texto: título, URL, data e trecho de cada resultado.
- `read_page(url, question)`: texto da página guardada; fora do índice, busca na hora com o mesmo
  extrator do crawler (um request em nome de uma pessoa, como um navegador) e põe a URL na frontier.
  Com `question` e LLM configurada, responde sobre a página. Até 20 mil caracteres.

Sem estado (`stateless_http=True`): sem sessão em memória, qualquer processo atende qualquer
request, o que permite vários workers do uvicorn ou réplicas atrás do Caddy.
O SDK recusa com 421 qualquer `Host` fora de `BINGUS_MCP_HOSTS` (padrão `localhost,127.0.0.1`),
proteção contra DNS rebinding. Nesta máquina: `localhost,127.0.0.1,10.10.0.50`. Para usar no Claude
Code: `claude mcp add --transport http bingus http://10.10.0.50:2026/mcp`.

## Frontend

Vite puro em `frontend/`, sem framework nem Bootstrap. Redesenhado em 23/11 a partir de dois
mockups do dono (início e resultados): tema escuro, claro pelo `prefers-color-scheme`, Inter, Poppins
no logo e JetBrains Mono nos números. Variáveis e peças comuns em `src/style.css`.

- `index.html` + `src/main.ts`: fogo em ASCII no fundo (`src/fire.ts`, ruído de Perlin; o cursor,
  o foco e a digitação atiçam), busca por voz (`src/voice.ts`, Web Speech API, botão some sem ela) e
  "Estou com sorte", que manda `sorte=1` e a página de resultados pula para o primeiro.
- `search.html` + `src/search.ts`: chama `GET /search` na API em `VITE_API_URL`, padrão
  `http://localhost:8000`. Chave "Resumos" só troca o que aparece; religada, refaz a busca se faltar
  resumo. Painel "Como essa busca rodou" com os `timings`. "Perguntar à página" chama `/page`.
- `mcp.html` + `src/mcp.ts`: como conectar o MCP (Claude Code, Claude, Cursor, VS Code, Codex,
  `mcp-remote`), as duas ferramentas, chave e o formato SearXNG. O endereço sai de `VITE_API_URL` + `/mcp`.

O dev server reescreve `/search` e `/mcp` para os `.html`; em produção o Dockerfile faz o build e
um Caddy serve `dist` com a mesma regra (`try_files {path}.html`).
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
bingus-rank`. Carrega páginas e links do Postgres por `COPY` direto em numpy, as duas leituras na
mesma transação `REPEATABLE READ` (com o crawler gravando entre elas, links de páginas novas caíam
fora da matriz e o rank morria), resolve os hashes de destino por busca binária, monta matriz
esparsa em scipy e roda 20 iterações com amortecimento
0.85 e tratamento de páginas sem links. Grava `pages.rank` e `hosts.rank` com média 1. Páginas de
redirect guardam o destino em `links` para repassar rank. Em 11 mil páginas leva 6 s; em 10
milhões estima-se minutos e uns 12 GB de RAM no pico.

Regra aprendida a caro: nunca um UPDATE só sobre a tabela `pages` inteira. Com 1,17 milhão de
linhas ele levou 45 minutos segurando o crawler travado e morreu em deadlock com o fetch worker.
Rank e seed gravam em lotes de 50 mil ids, cada um em transação própria.

O rank roda a cada 6 horas (`bingus-diario.timer`). Com 238 milhões de links ele morria sem memória
nos 14 GB do container da API: agora resolve os links por pedaço do COPY, direto em int32, e
calcula o peso por página; pico de 3,9 GB com 5,7 milhões de páginas. Grava só rank que mudou mais
de 5% (`RANK_TOLERANCE`): regravar tudo levava 63 minutos e 11 GB de NVMe por rodada, porque cada
UPDATE em pages é linha nova e entrada nova em cada índice, o BM25 incluso.

## Seeds

`POST /fetch/seed` recebe uma lista de URLs para a frontier. `seeds/brasil.txt` tem uns 200 sites
brasileiros variados por categoria (notícias, governo, saúde, tecnologia, receitas, educação,
esporte, cultura, dev...), a semente do banco desta máquina. A Wikipédia não é semeada: o dump
inteiro contra poucos milhares de sites reais fazia toda busca devolver Wikipédia. Ela entra pelos
links, com `max_pages` 20 mil. O carregador do dump continua aí para quando o resto do índice for
grande o bastante. Para a Wikipédia existe
`bingus-seed wikipedia data/ptwiki.tar.gz [pular N]`, em `bingus/api/seed.py`:

- Dump HTML do Wikimedia Enterprise, `ptwiki-NS0-<data>-ENTERPRISE-HTML.json.tar.gz` em
  `dumps.wikimedia.org/other/enterprise_html/runs/`. Uns 16 GB, 1,4 milhão de artigos. Lido em
  streaming, nunca extraído no disco. `data/` está no gitignore.
- Extração em processos filhos, `BINGUS_SEED_WORKERS`, padrão 4. No Windows cada processo
  importa tudo de novo, 16 processos estouraram a memória.
- Cada artigo passa por `ingest`, a mesma função dos resultados do crawler: chunks, twins,
  `links`, tudo igual. Idioma forçado para `pt`, porque o detector erra em artigo curto.
  Redirects e vazios são pulados. Sufixo "– Wikipédia, a enciclopédia livre" removido.
- Deadlock com o fetch worker acontece e é esperado: o lote é repetido até cinco vezes.
- Links internos não vão para a frontier, o dump já traz os artigos. Links externos só entram se
  o host termina em `.br`. Tudo ainda vai para `links`, então o PageRank vê o grafo inteiro.
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

A fonte é o Postgres: o Grafana do compose lê com a datasource Postgres, usando um usuário só de
leitura. Para o Prometheus da máquina há também o `GET /metrics`, ver abaixo. Três tabelas, todas
com retenção de 90 dias, limpas pela própria API:

- `worker_samples`: o worker faz push a cada ~10 s em `POST /metrics` com a mesma chave.
  Vai junto `instance`, o hostname ou `BINGUS_NAME`, e a API grava `worker` como `fetch/nome`:
  várias máquinas com a mesma chave viram séries separadas.
  Cada amostra traz requests, bytes baixados, páginas prontas, erros por tipo e CPU do processo
  no período. Workers não abrem porta nenhuma. O embed manda também `gpu` (% de uso), `gpu_mem`
  (bytes de VRAM) e `gpu_watts`, lidos do `nvidia-smi` no momento da amostra (`Stats(gpu=True)`);
  nos fetch workers ficam nulos, senão os nativos reportariam a GPU da máquina. O `/metrics`
  repete os três como `bingus_worker_gpu_*`.
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

O Grafana do usuário é o 13.1, com dashboards v2 (`dashboard.grafana.app/v2`). O dele está em
`grafana/bingus-v2.json`, mesmo uid, para importar por cima: o `bingus.json` com a correção do id
do plugin, mais páginas com texto, cobertura do embedding, banco, sites com texto e fatia nerd/tech
no topo, uma linha Recursos (GPU pelo `worker_samples` e máquina pelo node exporter, numa segunda
variável `PROM`), sites ordenados por `pt_count` (o `page_count` conta falhas: hardmob tinha 19 mil
visitadas e 2 mil com texto) e a família Radar. Contagens grandes vêm de estimativas do ANALYZE, e
total e parte saem do mesmo `reltuples`: misturar com `n_live_tup`, que é ao vivo, superestimou os
chunks com vetor em 50%. E de índices, não da tabela: o `reltuples` da tabela o VACUUM extrapola das
páginas que leu e ficou 2 milhões abaixo do real, enquanto o de cada índice ele conta inteiro. Chunks
com vetor é `chunks_pkey` menos `chunks_pending`; com o da tabela, o painel mostrou -289. Validado num Grafana 13.1.0 descartável: importação pela API v2 e todas as
consultas por `/api/ds/query`, que expande as macros de verdade.

### Prometheus

A máquina já tem Grafana, Prometheus central em `10.10.0.1:9090` (remote write aberto, sem
credencial, retenção de 30 dias) e um Alloy do sistema, que não é nosso, mandando o node exporter
com `instance="craftsapiens"` e `job="craftsapiens"`. O Bingus não mexe nele: o compose tem um
Alloy próprio (`alloy/config.alloy`, imagem fixa) que raspa `GET /metrics` da API a cada 15 s
pela rede do compose e faz remote write para o mesmo Prometheus, com a mesma `instance` e
`job="bingus"`. Job igual ao do node exporter faria o `up` dos dois Alloys virar uma série só.

- `GET /metrics` pede uma chave como os workers: `metrics:<chave>` em `BINGUS_API_KEYS` e a mesma
  chave em `BINGUS_METRICS_KEY`, que o compose passa ao Alloy. A API é pública pelo Caddy.
- Formato texto do Prometheus escrito à mão em `api/metrics.py`, sem dependência. Custa duas
  consultas de poucos milissegundos: `n_live_tup` de `pg_stat_user_tables` para as tabelas,
  `reltuples` dos índices parciais de chunks, `n_live_tup` de `texts` para páginas com texto,
  a última linha de `api_samples` para hosts vencidos e em lease, e os tamanhos.
  Depois de uma queda do Postgres o `n_live_tup` zera até o próximo ANALYZE.
- Os contadores (`*_total`) vivem na memória da API, alimentados por `record_batch` e pelo
  `POST /metrics` dos workers, e zeram quando ela reinicia; o `rate()` entende. Somar a tabela
  `batches` não serve: a limpeza dos 90 dias faria o total cair, e o Prometheus leria a queda como
  restart, com um pico do tamanho do total. Lotes do `bingus-seed`, processo à parte, não entram.
- Dashboard em `grafana/bingus-prometheus.json`, datasource Prometheus na variável `DS` e
  máquina em `instance`, com uma linha de node exporter no fim.

## Deploy

Imagens no Docker Hub: `shiftsad/bingus:api`, `:fetch`, `:embed`. O compose usa as imagens e sabe
construí-las do código (`docker compose --profile workers build`; nesta máquina não há login no Hub,
então as imagens são locais). O perfil `workers` sobe fetch e embed na mesma máquina, o embed com a
GPU via toolkit da NVIDIA.

Na máquina compartilhada:

- Todo serviço tem `cgroup_parent: bingus.slice`, `restart: unless-stopped` e um `mem_limit` próprio.
  Os limites somam mais que 32 GB de propósito: o slice é o teto real, eles só impedem um serviço de
  engolir os outros. Testado: estourar o slice mata só o processo de dentro.
- `systemd/` tem o slice e dois timers do usuário: `bingus-diario` (09:10 UTC: rank, reindex do BM25
  e vacuum) e `bingus-vigia` (a cada 10 min: pausa os fetch workers com o banco em 68 GB e retoma só o
  que ele pausou). Os 68 saem do teto de 100 GB de NVMe para tudo do Bingus: ~19 GB fora do banco
  (imagens ~15, modelo 2,3, código e caches) e ~9 GB de vetores que a fila do embed ainda grava
  depois da pausa (~1,1 KB por chunk). O cache de construção do Docker chegou a 36 GB num dia de
  rebuilds: depois de reconstruir imagem, `docker builder prune -af` e `docker image prune -f`.
  Do Docker deste usuário, `gpu_burn` e `mariadb` (geopolitico-db, craftsapiens-db) não são do
  Bingus: não mexer. Instalação no cabeçalho de `systemd/bingus.slice`. Depende do linger do usuário,
  ativado, senão tudo morre quando as sessões fecham.
- Boot: Docker rootless, os fetch workers habilitados e os timers sobem com o systemd do usuário, e os
  containers têm `restart: unless-stopped`. Só que o Docker restaura os containers antes da WireGuard
  subir, e porta publicada num IP inexistente impede o container de iniciar (testado: "cannot assign
  requested address"). `bingus.service` roda `systemd/sobe.sh`, que espera os IPs dos `*_BIND` do
  `.env` e faz `compose --profile workers up -d --no-recreate`: só liga o que ficou parado.
  Testado de verdade em 19/11, com dois cortes de energia (14:21 e 14:24 UTC): o Postgres se
  recuperou sozinho, a API reiniciou 13 vezes até ele aceitar conexões, e às 17:33 a ingestão já
  tinha voltado; uns 12 minutos fora no total. O `docker logs` do container restaurado não mostra
  as linhas depois do boot; o banco (`batches`, `chunks.embedded_at`) é a prova de que voltou.
  O log inteiro está no arquivo de `docker inspect -f '{{.LogPath}}'`.
- Reboot com `sudo reboot` também mata o Postgres: o Ubuntu traz
  `/usr/lib/systemd/system/user@.service.d/timeout.conf` com `TimeoutStopSec=5`, e o systemd para os
  scopes dos containers direto, sem o `stop_grace_period`. O Postgres morreu 1 s depois do pedido de
  desligamento; na subida, 169 s de redo e 264 s do checkpoint de fim de recuperação (fsync no HD),
  primeira página 8 min depois do boot. Correções: `checkpoint_timeout=5min` e `max_wal_size=2GB`
  (pouco WAL para refazer), a API espera o banco em `db.connect` em vez de cair no backoff de
  restart do Docker, e, com sudo, `user@1001.service.d/zz-bingus.conf` com `TimeoutStopSec=180`.
- `gpu-unlock` 2.0.0 (dkms e firmware) instalado pelo usuário em 19/11 18:05 UTC. Embed de 31-34
  para ~42 chunks/s na 3060 Ti nos primeiros minutos, longe dos 2x prometidos.
- `.env` daqui, além do que vem abaixo: `BINGUS_PG_DATA=/home/sadness/bingus-data/pg`,
  `BINGUS_HF_CACHE=/home/sadness/.cache/bingus-hf`, `BINGUS_DATA=/mnt/hdd/bingus` (vira `/data` na
  API), `BINGUS_FETCH_REPLICAS=0` (workers nativos, ver Protocolo), `BINGUS_MCP_HOSTS`,
  `BINGUS_METRICS_KEY` (ver Prometheus, em Métricas), binds em
  `10.10.0.50` portas 2026 (API), 2027 (web), 2028 (Grafana) e 5432 (Postgres, para o Grafana do
  usuário, como era no dedicado). A porta 8080 já é usada por outro serviço da máquina. O Docker
  rootless entrega as conexões da porta publicada vindas de 172.19.0.1, então o `trust` do
  pg_hba para 127.0.0.1 não vale para quem vem de fora: tudo pede senha. A máquina tem ufw ativo,
  e porta publicada pelo Docker rootless passa por ele, ao contrário do Docker com root: porta nova
  dá timeout de fora até alguém com sudo liberar, como
  `ufw allow in on wg0 from 10.10.0.0/24 to 10.10.0.50 port 5432 proto tcp` em 19/11. O Postgres tem
  `stop_grace_period: 2m`: com `checkpoint_timeout` de 30 min, os 10 s padrão acabavam em SIGKILL.
- No HD só as tabelas `texts` e `links`. Testado em 19/11: a tabela `pages` inteira num tablespace no HD (índices
  no NVMe) fez cada busca BM25 levar minutos, porque o scan lê milhares de blocos das linhas e do
  TOAST, onde mora o vetor `bm25`. As buscas seguravam locks de buffer e travavam a ingestão. O scan
  do `vchord_bm25` não atende `pg_cancel_backend` nem `pg_terminate_backend`; só `kill -9` no
  processo, e o Postgres faz uma recuperação curta. O TOAST não vai sozinho para outro tablespace,
  porque é catálogo do sistema. Por isso o texto saiu de `pages` para `texts`, no tablespace `hdd`
  (`/tablespace/data` no container, `BINGUS_PG_TABLESPACE=/mnt/hdd/bingus/pg` no host). A busca lê
  o texto só dos resultados mostrados, o embed lê os chunks alugados, e a amostra de grafias lê ~2
  mil blocos por hora. Por página com texto são ~2,7 KB de texto comprimido (agora no HD), ~2,7 KB
  de vetor BM25, ~3,2 KB de índice BM25 e ~0,5 KB de links. Os links saíram de `pages.out_links`
  para `links`, no HD, também em 19/11: eram 2,3 GB, 40% do heap de `pages`, lidos só pelo
  PageRank, de ponta a ponta. O HD lê à frente 8 MB (`read_ahead_kb`), então o scan sequencial não
  esbarra nas 100 leituras por segundo. Com o mount sumindo, o container continua de pé apontando
  para a pasta apagada e o Postgres não grava em `texts` nem em `links`: recriar com
  `--force-recreate --no-deps postgres`. `max_wal_size` é 2 GB: com 16 GB, o WAL ocupava 16 GB.
- Grafana no compose (`grafana/provisioning`): datasource com o usuário `grafana` só de leitura,
  dashboard `grafana/bingus.json` como home, anônimo como Viewer porque só a WireGuard alcança.
  O dashboard pedia o plugin `grafana-postgres-datasource`; o id real é `grafana-postgresql-datasource`.
- `docker compose up -d` de um serviço recria as dependências que mudaram: o Postgres foi recriado
  por tabela depois de uma mudança no healthcheck. Para não mexer nelas, `up -d --no-deps`.
- PageRank em 15 mil páginas: agências que assinam o rodapé dos clientes ("feito por") sobem para o
  topo dos hosts. Pouco estrago na busca, onde o rank pesa metade e só entre candidatos; contar um
  link por par de hosts resolveria.
- O diretório de dados do Postgres precisa de permissão 755 no host: com 700 o usuário `postgres` do
  container, mapeado num subuid, não entra e o container reinicia em loop.
- Postgres com `shared_buffers` 8 GB, `effective_cache_size` 20 GB, `synchronous_commit=off` (uma
  queda perde segundos que o crawler refaz) e `shm_size` 2 GB.

Deploy no dedicado antigo:

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

CommonCrawl, notícias, RSS, PDF, reescrita da busca, sumários. Separar de `pages` o que muda a
cada visita (`next_check_at`, `rank` e afins): são colunas indexadas, então toda revisita e todo
PageRank inserem a linha de novo nos sete índices, BM25 incluído. Deve explicar boa parte do inchaço
do índice bm25. `gpu-unlock` (dobra BF16 na 3060 Ti) só age no boot, e a máquina não reinicia.

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
- A coluna `bm25` é gravada pelo `SAVE_CONTENT` da ingestão com `tokenize($texto, 'pt')`, no mesmo
  UPDATE. Antes era um trigger sobre `pages.text`, que saiu para `texts`.
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
