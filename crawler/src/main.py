import time
from bs4 import BeautifulSoup

import asyncio
import aiohttp

from crawler.src.postgres import insert_url

crawledUrls = set()
queue = asyncio.Queue()


async def worker(session):
    while True:
        url = await queue.get()

        if not isValidLink(url):
            queue.task_done()
            continue

        if url in crawledUrls:
            queue.task_done()
            continue

        crawledUrls.add(url)
        print(f"Crawled {len(crawledUrls)} URLs, Running: {url}")

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"Failed to fetch {url}: {response.status}")
                    queue.task_done()
                    continue

                soup = BeautifulSoup(await response.text(), 'html.parser')
                links = soup.find_all('a', href=True)
                plainText = soup.get_text()

                await insert_url(url, plainText)

                for link in links:
                    await queue.put(link['href'])
        
        except Exception as e:
            print(f"Error fetching {url}: {e}")
        finally:
            queue.task_done()


def isValidLink(link):
    # If image or file...
    if link.endswith(('.png', '.jpg', '.jpeg', '.gif', '.pdf', '.docx', '.xlsx')):
        return False
    
    # If link is a fragment
    if link.startswith('#'):
        return False
    
    if not link.startswith('http'):
        return False
    
    return True


async def main():
    startingPoints = [
        "https://pt.wikipedia.org/wiki/Minecraft"
    ]

    num_workers = 20

    async with aiohttp.ClientSession() as session:
        for url in startingPoints:
            await queue.put(url)

        tasks = []
        for i in range(num_workers):
            task = asyncio.create_task(worker(session))
            tasks.append(task)

        await queue.join()
        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)        


if __name__ == "__main__":
    start_time = time.time()
    asyncio.run(main())
    end_time = time.time()

    print("\n--- Crawling Complete ---")
    print(f"Total URLs crawled: {len(crawledUrls)}")
    print(f"Total time taken: {end_time - start_time:.2f} seconds")
    print("\nCrawled URLs:")
    for url in crawledUrls:
        print(url)