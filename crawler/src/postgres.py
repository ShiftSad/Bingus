import asyncpg
import os
import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:banana@localhost:5432/postgres")
POOL = None

conn = psycopg2.connect(DATABASE_URL)

async def get_pool():
    global POOL
    if POOL is None:
        POOL = await asyncpg.create_pool(DATABASE_URL, min_size=5, max_size=20)
    return POOL


async def get_connection():
    pool = await get_pool()
    conn = await pool.acquire()
    return conn


def create_table():
    with conn.cursor() as cursor:
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
                FOREIGN KEY (from_url) REFERENCES crawled_urls(url),
                FOREIGN KEY (to_url) REFERENCES crawled_urls(url)
            )
        """)
        with open("calculate_pagerank.sql", "r") as f:
            sql = f.read()
            cursor.execute(sql)

        conn.commit()


def getCrawledUrls(crawledUrls):
    cursor = conn.cursor()
    cursor.execute("SELECT url FROM crawled_urls")
    rows = cursor.fetchall()
    for row in rows:
        crawledUrls.add(row[0])
    cursor.close()


async def insert_url(url, plaintext):
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute("""
                INSERT INTO crawled_urls (url, plaintext)
                VALUES ($1, $2)
                ON CONFLICT (url) DO NOTHING
            """, url, plaintext)
        except Exception as e:
            print(f"Error inserting URL {url}: {e}")


async def insert_url_links(from_url, to_urls):
    if not to_urls:
        return
    
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            values = [(from_url, to_url) for to_url in to_urls]

            await conn.execute_many("""
                INSERT INTO urls (from_url, to_url)
                VALUES ($1, $2)
                ON CONFLICT (from_url, to_url) DO NOTHING
            """, values)
            
        except Exception as e:
            print(f"Error inserting URL links from {from_url}: {e}")