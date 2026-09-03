-- Aplicado uma vez pelo initdb do container.
-- Mudou o schema: docker compose down -v && docker compose up -d

CREATE EXTENSION IF NOT EXISTS vchord CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_tokenizer CASCADE;
CREATE EXTENSION IF NOT EXISTS vchord_bm25 CASCADE;

CREATE TABLE hosts (
    host         text PRIMARY KEY,
    robots       text,                        -- robots.txt bruto, null = nunca buscado
    robots_at    timestamptz,
    crawl_delay  real NOT NULL DEFAULT 1.0,   -- segundos entre requests neste host
    langs        text[] NOT NULL DEFAULT '{pt}', -- idiomas aceitos; .br e fontes de IA levam {pt,en}
    max_pages    integer NOT NULL DEFAULT 1000000, -- válvula contra sites infinitos, só para baixo
    page_count   integer NOT NULL DEFAULT 0,   -- páginas conhecidas, inclusive falhas
    pt_count     integer NOT NULL DEFAULT 0,   -- páginas com conteúdo em português
    foreign_count integer NOT NULL DEFAULT 0,  -- páginas em outro idioma
    sitemaps_at  timestamptz,                  -- null = sitemaps ainda não lidos
    rank         real NOT NULL DEFAULT 0,
    blocked      boolean NOT NULL DEFAULT false,
    next_due     timestamptz NOT NULL DEFAULT now(),  -- trabalho mais antigo pendente no host
    leased_until timestamptz                  -- host entregue a um fetch worker
);
CREATE INDEX hosts_due ON hosts (next_due) WHERE NOT blocked;

-- URLs descobertas e nunca buscadas. A linha some quando vira página.
CREATE TABLE frontier (
    url_hash bigint PRIMARY KEY,              -- xxh3-64 da URL normalizada
    url      text NOT NULL,
    host     text NOT NULL,
    depth    smallint NOT NULL DEFAULT 0,
    added_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX frontier_host ON frontier (host, added_at);

CREATE TABLE pages (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url_hash        bigint NOT NULL UNIQUE,
    url             text NOT NULL,
    host            text NOT NULL REFERENCES hosts,
    depth           smallint NOT NULL DEFAULT 0,
    title           text,
    text            text,                     -- texto extraído, título na primeira linha
    summary         text,                     -- gerado por LLM só quando a página é acessada
    lang            text,
    published_at    date,                     -- data de publicação extraída da página
    status          smallint,                 -- último status HTTP
    fetched_at      timestamptz,
    last_changed_at timestamptz,
    next_check_at   timestamptz NOT NULL DEFAULT now(),
    check_interval  interval NOT NULL DEFAULT '7 days',
    etag            text,
    last_modified   text,
    content_hash    bytea,                    -- sha256 do texto extraído
    fail_count      smallint NOT NULL DEFAULT 0,
    rank            real NOT NULL DEFAULT 0,
    out_links       bigint[],                 -- url_hash das páginas apontadas
    bm25            bm25vector
);
ALTER TABLE pages ALTER COLUMN text SET COMPRESSION lz4;
CREATE INDEX pages_host_due ON pages (host, next_check_at) WHERE fail_count < 10;
CREATE INDEX pages_content ON pages (content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX pages_bm25 ON pages USING bm25 (bm25 bm25_ops);
CREATE INDEX pages_rank ON pages (rank DESC);  -- embed worker pega as melhores páginas antes
-- Páginas rejeitadas por idioma: só lang e data, sem conteúdo. Para o dashboard.
CREATE INDEX pages_foreign ON pages (fetched_at DESC) INCLUDE (lang)
WHERE content_hash IS NULL AND lang IS NOT NULL;

-- Fatias de pages.text. Embedding nulo = fila do embed worker.
CREATE TABLE chunks (
    page_id      bigint NOT NULL REFERENCES pages ON DELETE CASCADE,
    seq          smallint NOT NULL,
    start_ch     integer NOT NULL,
    end_ch       integer NOT NULL,
    simhash      bigint NOT NULL,             -- do texto embedado, só muda ao reembedar
    embedding    rabitq8(512),
    embedded_at  timestamptz,
    leased_until timestamptz,
    PRIMARY KEY (page_id, seq)
);
CREATE INDEX chunks_pending ON chunks (page_id) WHERE embedding IS NULL;
CREATE INDEX chunks_embedded_at ON chunks (embedded_at DESC) WHERE embedded_at IS NOT NULL;
CREATE INDEX chunks_embedding ON chunks USING vchordrq (embedding rabitq8_cosine_ops)
WITH (options = $$
[build.internal]
lists = []
spherical_centroids = true
$$);

-- Telemetria. Grafana lê direto daqui com a datasource Postgres. Retenção de 90 dias.

-- Enviada pelos workers a cada ~10 s em POST /metrics.
CREATE TABLE worker_samples (
    worker   text NOT NULL,
    at       timestamptz NOT NULL,
    requests integer NOT NULL,       -- no período da amostra
    bytes    bigint NOT NULL,
    pages    integer NOT NULL,       -- resultados prontos no período
    errors   jsonb NOT NULL,         -- {"timeout": 2, "http_5xx": 1}
    cpu      real NOT NULL,          -- núcleos usados pelo processo
    PRIMARY KEY (worker, at)
);

-- Uma linha por resultado ingerido pela API.
CREATE TABLE batches (
    worker          text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now(),
    ms              integer NOT NULL,   -- tempo de ingestão
    pages_new       integer NOT NULL,
    pages_changed   integer NOT NULL,
    pages_unchanged integer NOT NULL,
    pages_failed    integer NOT NULL,
    pages_foreign   integer NOT NULL,
    frontier_added  integer NOT NULL,
    chunks_queued   integer NOT NULL,
    chunks_embedded integer NOT NULL,   -- lotes do embed worker
    PRIMARY KEY (worker, at)
);

-- Estado geral, uma linha por minuto.
CREATE TABLE api_samples (
    at             timestamptz PRIMARY KEY DEFAULT now(),
    frontier       bigint NOT NULL,    -- estimativas do planner
    pages          bigint NOT NULL,
    chunks_pending bigint NOT NULL,
    hosts          integer NOT NULL,
    hosts_due      integer NOT NULL,
    hosts_leased   integer NOT NULL
);

-- BM25 com o tokenizador do Gemma: vocabulário fixo de 256 mil tokens, multilíngue.
-- Vocabulário customizado cresce sem limite e o índice bm25 gasta 8 KB por termo:
-- com a Wikipédia deu 1,5 milhão de termos e 12 GB de índice para 1 GB de texto.
SELECT tokenizer_catalog.create_tokenizer('pt', $$
model = "gemma2b"
[[character_filters]]
to_lowercase = {}
$$);

CREATE FUNCTION pages_bm25() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    NEW.bm25 := tokenizer_catalog.tokenize(NEW.text, 'pt')::bm25vector;
    RETURN NEW;
END $f$;
CREATE TRIGGER pages_bm25 BEFORE INSERT OR UPDATE OF text ON pages
    FOR EACH ROW EXECUTE FUNCTION pages_bm25();
