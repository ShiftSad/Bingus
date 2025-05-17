import psycopg2

conn = psycopg2.connect(
    database = "postgres",
    user = "user",
    password = "banana",
    port = 5432,
    host = "localhost"
)


def create_table():
    with conn.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crawled_urls (
                id SERIAL PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                embedding vector(768),
                plaintext TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT url_length CHECK (char_length(url) <= 2048)
            )
        """)
        conn.commit()


async def insert_url(url, plaintext):
    with conn.cursor() as cursor:
        try:
            cursor.execute("""
                INSERT INTO crawled_urls (url, plaintext)
                VALUES (%s, %s)
                ON CONFLICT (url) DO NOTHING
            """, (url, plaintext))
            conn.commit()
        except Exception as e:
            print(f"Error inserting URL {url}: {e}")