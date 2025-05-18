import asyncio
import asyncpg
import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:banana@localhost:5432/postgres")
POOL = None

sync_conn = psycopg2.connect(DATABASE_URL)

async def get_pool():
    global POOL
    if POOL is None:
        POOL = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=5,
            max_size=20,
            max_inactive_connection_lifetime=300.0,
        )
    return POOL


async def get_connection():
    pool = await get_pool()
    conn = await pool.acquire()
    return conn


def create_table():
    with sync_conn.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crawled_urls (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                embedding vector(768),
                plaintext TEXT,
                rank FLOAT DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT url_length CHECK (char_length(url) <= 2048)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS urls (
            from_url TEXT NOT NULL,
            to_url TEXT NOT NULL,
            UNIQUE (from_url, to_url),
            CONSTRAINT no_self_references CHECK (from_url <> to_url)
            )
        """)

        try:
            with open("calculate_pagerank.sql", "r", encoding="utf-8") as f:
                sql = f.read()
                cursor.execute(sql)
        except UnicodeDecodeError:
            with open("calculate_pagerank.sql", "r", encoding="latin-1") as f:
                sql = f.read()
                cursor.execute(sql)

        sync_conn.commit()


def getCrawledUrls(crawledUrls):
    cursor = sync_conn.cursor()
    cursor.execute("SELECT url FROM crawled_urls")
    rows = cursor.fetchall()
    for row in rows:
        crawledUrls.add(row[0])
    cursor.close()


def getPlainText(limit=1000):
    cursor = sync_conn.cursor()
    cursor.execute("SELECT url, plaintext FROM crawled_urls WHERE plaintext IS NOT NULL AND embedding IS NULL LIMIT %s", (limit,))
    rows = cursor.fetchall()
    cursor.close()
    return rows


async def saveEmbedding(url, embedding):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            embedding_str = ','.join(map(str, embedding))
            embedding_str = f'[{embedding_str}]'
            
            await conn.execute(
                """
                UPDATE crawled_urls
                SET embedding = $1::vector
                WHERE url = $2
                """,
                embedding_str,
                url,
            )
        except Exception as e:
            print(f"Error saving embedding for {url}: {e}")


class InsertType:
    URL = "url"
    URL_LINKS = "url_links"


class InsertTask:
    def __init__(self, insert_type, data):
        self.insert_type = insert_type
        self.data = data


class InsertPool:
    def __init__(self, max_queue_size=10000, batch_size=100, flush_interval=1.0):
        self.queue = asyncio.Queue(maxsize=max_queue_size)
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.worker_task = None
        self.running = False


    async def start(self):
        if not self.running:
            self.running = True
            self.worker_task = asyncio.create_task(self.worker())


    async def stop(self):
        self.running = False
        if self.worker_task:
            await self.worker_task


    async def insert_url(self, url, plaintext):
        await self.queue.put(InsertTask(InsertType.URL, (url, plaintext)))


    async def insert_url_links(self, from_url, to_urls):
        if to_urls:
            await self.queue.put(InsertTask(InsertType.URL_LINKS, (from_url, to_urls)))


    async def worker(self):
        while self.running:
            batch = []
            try:
                # Wait for at least one item
                task = await asyncio.wait_for(self.queue.get(), timeout=self.flush_interval)
                batch.append(task)
            except asyncio.TimeoutError:
                pass

            # Gather up to batch_size items
            while len(batch) < self.batch_size and not self.queue.empty():
                batch.append(self.queue.get_nowait())

            if batch:
                await self.flush(batch)


    async def flush(self, batch):
        pool = await get_pool()
        url_inserts = []
        url_links_inserts = []

        for task in batch:
            if task.insert_type == InsertType.URL:
                url_inserts.append(task.data)
            elif task.insert_type == InsertType.URL_LINKS:
                from_url, to_urls = task.data
                for to_url in to_urls:
                    url_links_inserts.append((from_url, to_url))
                    url_inserts.append((to_url, None))

        async with pool.acquire() as conn:
            url_inserts = [(url, plaintext) for url, plaintext in url_inserts if url and (plaintext is not None)]
            if url_inserts:
                try:
                    # First insert all URLs to satisfy foreign key constraints
                    await conn.executemany(
                        """
                        INSERT INTO crawled_urls (url, plaintext)
                        VALUES ($1, $2)
                        ON CONFLICT (url) DO UPDATE SET
                            plaintext = COALESCE(EXCLUDED.plaintext, crawled_urls.plaintext)
                        """,
                        url_inserts,
                    )
                except Exception as e:
                    print(f"Error batch inserting URLs: {e}")

            if url_links_inserts:
                try:
                    # Then insert the relationships
                    await conn.executemany(
                        """
                        INSERT INTO urls (from_url, to_url)
                        VALUES ($1, $2)
                        ON CONFLICT (from_url, to_url) DO NOTHING
                        """,
                        url_links_inserts,
                    )
                except Exception as e:
                    print(f"Error batch inserting URL links: {e}")