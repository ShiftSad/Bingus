from sentence_transformers import SentenceTransformer
import torch
import asyncio
import asyncpg
import os
from typing import List, Tuple

# Use the same model as in embedding.py
device = 'cuda' if torch.cuda.is_available() else 'cpu'
model = SentenceTransformer('sentence-transformers/all-mpnet-base-v2')
model = model.to(device)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:banana@localhost:5432/postgres")

async def search(query: str, limit: int = 10) -> List[Tuple[str, float, str]]:
    """
    Search for documents similar to the query
    
    Args:
        query: The search query
        limit: Maximum number of results to return
        
    Returns:
        List of tuples containing (url, score, snippet)
    """
    # Get embedding for the query
    embedding = model.encode(query, show_progress_bar=False, device=device)
    embedding_str = ','.join(map(str, embedding.tolist()))
    embedding_vector = f'[{embedding_str}]'
    
    # Connect to database
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        # Query using cosine similarity, combining with PageRank for better results
        results = await conn.fetch(
            """
            SELECT 
            url,
            CASE WHEN 1 - (embedding <=> $1::vector) = 1 THEN 0
             ELSE 1 - (embedding <=> $1::vector)
            END AS similarity,
            rank AS pagerank,
            plaintext
            FROM 
            crawled_urls
            WHERE 
            embedding IS NOT NULL
            AND 1 - (embedding <=> $1::vector) < 1.0
            ORDER BY 
            (0.7 * (CASE WHEN 1 - (embedding <=> $1::vector) = 1 THEN 0
                 ELSE 1 - (embedding <=> $1::vector)
                END)) + (0.3 * rank) DESC
            LIMIT $2
            """,
            embedding_vector,
            limit
        )
        
        # Process results
        search_results = []
        for row in results:
            url = row['url']
            score = row['similarity'] 
            text = row['plaintext']
            
            # Create a snippet (first 200 chars)
            snippet = text[:200] + "..." if len(text) > 200 else text
            
            search_results.append((url, float(score), snippet))
        
        return search_results
    
    finally:
        await conn.close()

async def main():
    while True:
        query = input("Enter search query (or 'exit' to quit): ")
        if query.lower() == 'exit':
            break
            
        results = await search(query)
        
        print(f"\nSearch results for '{query}':\n")
        for i, (url, score, snippet) in enumerate(results, 1):
            print(f"{i}. {url} (Score: {score:.4f})")
            print(f"   {snippet}\n")

if __name__ == "__main__":
    asyncio.run(main())