import time
import pickle
import os
from bs4 import BeautifulSoup

import asyncio
import aiohttp

from postgres import *
create_table()

crawledUrls = set()
getCrawledUrls(crawledUrls)

queue = asyncio.Queue()
QUEUE_SAVE_PATH = "crawler_queue.pickle"
SAVE_INTERVAL = 300
USER_AGENT = "ShiftCrawler/1.0 (+https://github.com/ShiftSad/SearchEngine)"

insert_pool = InsertPool(max_queue_size=10000, batch_size=100, flush_interval=1.0)

def load_queue():
    if os.path.exists(QUEUE_SAVE_PATH):
        try:
            with open(QUEUE_SAVE_PATH, 'rb') as f:
                urls = pickle.load(f)
                print(f"Loaded {len(urls)} URLs from saved queue")
                return urls
        except Exception as e:
            print(f"Error loading queue: {e}")
    return []


async def save_queue():
    try:
        queue_items = []
        # Use queue._queue to peek at items without removing them
        queue_items = list(queue._queue)
        with open(QUEUE_SAVE_PATH, 'wb') as f:
            pickle.dump(queue_items, f)
            print(f"Saved {len(queue_items)} URLs to queue file")
    except Exception as e:
        print(f"Error saving queue: {e}")


async def periodic_save():
    while True:
        await asyncio.sleep(SAVE_INTERVAL)
        await save_queue()


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

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    print(f"Failed to fetch {url}: {response.status}")
                    queue.task_done()
                    continue

                if 'text/html' not in response.headers.get('Content-Type', ''):
                    print(f"Skipping non-HTML content: {url}")
                    queue.task_done()
                    continue

                soup = BeautifulSoup(await response.text(), 'html.parser')
                links = soup.find_all('a', href=True)
                plainText = soup.get_text()
                plainText = plainText.replace('\n', ' ').replace('\r', ' ').strip()

                if plainText:
                    await insert_pool.insert_url(url, plainText)

                valid_to_urls = []
                for link in links:
                    link_url = link['href']
                    if isValidLink(link_url):
                        # If link is self, skip it
                        if link_url == url:
                            continue
                        valid_to_urls.append(link_url)
                        await queue.put(link_url)

                await insert_pool.insert_url_links(url, valid_to_urls)

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
    saved_urls = load_queue()
    
    if not saved_urls:
        startingPoints = [
            "https://pt.wikipedia.org/wiki/Minecraft",
            "https://docs.python.org/3.9/library/asyncio-task.html",
            "https://medium.com/@luanrubensf/concurrent-map-access-in-go-a6a733c5ffd1",
        ]
        saved_urls.extend(startingPoints)

    num_workers = 40
    await insert_pool.start()

    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}
    ) as session:
        for url in saved_urls:
            await queue.put(url)

        save_task = asyncio.create_task(periodic_save())
        
        tasks = []
        for i in range(num_workers):
            task = asyncio.create_task(worker(session))
            tasks.append(task)

        try:
            await queue.join()
        except (KeyboardInterrupt, Exception) as e:
            print(f"Interrupted: {e}. Saving queue before exit...")
        finally:
            await save_queue()
            
            save_task.cancel()
            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)
            await insert_pool.stop()


if __name__ == "__main__":
    start_time = time.time()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Keyboard interrupt received, exiting gracefully...")
    end_time = time.time()

    print("\n--- Crawling Complete ---")
    print(f"Total URLs crawled: {len(crawledUrls)}")
    print(f"Total time taken: {end_time - start_time:.2f} seconds")