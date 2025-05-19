from postgres import *
import asyncio
import aiohttp
from bs4 import BeautifulSoup

async def fetch_title(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                title = soup.title.string if soup.title else 'No title'
                return [
                    url,
                    title,
                ]
            else:
                # print(f"Failed to fetch {url}: {response.status}")
                return None


async def main():
    urls = getUrlsWithoutTitleCount(100)
    if not urls:
        print("No more plain texts to process. Waiting before next attempt...")
        await asyncio.sleep(10)
        await main()
        return
        
    print(f"Fetching titles for {len(urls)} URLs...")
    
    # Fetch all URLs concurrently
    tasks = [fetch_title(url) for url in urls]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filter out errors and None results
    titles = []
    for i, result in enumerate(results):
        # if isinstance(result, Exception):
        #     # print(f"Error fetching {urls[i]}: {result}")
        if result is not None:
            titles.append(result)
            print(f"Got title {result[1]} ({len(titles)}/{len(urls)})")
    
    # Filter all null results
    titles = [title for title in titles if title is not None]

    if titles:
        print(f"Got {len(titles)} titles")
        await insert_titles(titles)
    await main()


asyncio.run(main())