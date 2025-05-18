from sentence_transformers import SentenceTransformer
from postgres import *
import torch

device = 'cuda' if torch.cuda.is_available() else 'cpu'
print(f"Using device: {device}")

model = SentenceTransformer('sentence-transformers/all-mpnet-base-v2')
model = model.to(device)

async def embed_text(url: str, text: str):
    embeddings = model.encode(text, show_progress_bar=False, device='cuda')
    embeddings_list = embeddings.tolist()
    await saveEmbedding(url, embeddings_list)


async def main():
    plainTexts = getPlainText(1000)
    count = 0
    
    for url, text in plainTexts:
        try:
            await embed_text(url, text)
            count += 1
            print(f"Embedded {url} ({count}/{len(plainTexts)})")
        except Exception as e:
            print(f"Error embedding {url}: {e}")

    await asyncio.sleep(0.5)
    await main()


asyncio.run(main())