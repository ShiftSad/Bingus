import asyncio
import gzip
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.robotparser import RobotFileParser

import httpx

from bingus.common.client import Api
from bingus.common.stats import INSTANCE, Stats
from bingus.common.urls import normalize
from bingus.fetch.page import extract

log = logging.getLogger("bingus.fetch")

UA = os.environ.get("BINGUS_USER_AGENT", "Bingus/1.0 (+https://github.com/ShiftSad/Bingus)")
HOSTS = int(os.environ.get("BINGUS_HOSTS", "20"))
PER_HOST = int(os.environ.get("BINGUS_PER_HOST", "10"))
FLUSH = int(os.environ.get("BINGUS_FLUSH", "15"))  # segundos entre envios de resultados
TIMEOUT = 15
MAX_BYTES = 5 * 1024 * 1024
MAX_SITEMAPS = 5
MAX_SITEMAP_URLS = 2000
ROBOTS_BLOCKED = 999  # status inventado: a API marca a página como morta
LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I | re.S)
# lxml em várias threads ao mesmo tempo dá segfault. Uma thread só para extrair.
EXTRACT = ThreadPoolExecutor(max_workers=1)


class Worker:
    def __init__(self, api: Api, stats: Stats) -> None:
        self.api = api
        self.stats = stats
        self.http = httpx.AsyncClient(
            headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True, max_redirects=5
        )
        self.tasks: set[asyncio.Task[None]] = set()  # um por host em voo
        self.freed = asyncio.Event()
        self.done: list[dict[str, Any]] = []  # hosts prontos, esperando o próximo envio

    async def run(self) -> None:
        """Mantém HOSTS hosts em voo. Cada um libera o slot ao terminar, em vez de um lote
        inteiro esperar o host mais lento. Resultados vão juntos, a cada FLUSH segundos."""
        while True:
            free = HOSTS - len(self.tasks)
            if free < max(1, HOSTS // 4):  # pede em lotes, não um host por vez
                self.freed.clear()
                await self.freed.wait()
                continue
            try:
                batch = await self.api.post_json(
                    f"/fetch/batch?hosts={free}&per_host={PER_HOST}", {}
                )
            except Exception as e:
                log.warning("batch failed: %s", e)
                await asyncio.sleep(30)
                continue
            if not batch["hosts"]:
                log.info("no work, waiting")
                await asyncio.sleep(30)
                continue
            log.info("%d hosts leased, %d in flight", len(batch["hosts"]), len(self.tasks))
            for h in batch["hosts"]:
                task = asyncio.create_task(self.crawl(h))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    async def crawl(self, h: dict[str, Any]) -> None:
        try:
            self.done.append(await self.crawl_host(h))
        except Exception as e:
            log.warning("%s failed: %s", h["host"], e)
        finally:
            self.freed.set()

    async def flush(self) -> None:
        while True:
            await asyncio.sleep(FLUSH)
            results, self.done = self.done, []
            if not results:
                continue
            pages = [p for r in results for p in r["pages"]]
            hosts = {r["host"]: r["info"] for r in results if r["info"]}
            urls = [u for r in results for u in r["urls"]]
            try:
                await self.api.post_json(
                    "/fetch/results", {"pages": pages, "hosts": hosts, "urls": urls}
                )
                log.info("%d hosts, %d pages, %d sitemap urls", len(results), len(pages), len(urls))
            except Exception as e:
                log.warning("results failed: %s", e)

    async def crawl_host(self, h: dict[str, Any]) -> dict[str, Any]:
        host, delay, robots = h["host"], h["crawl_delay"], h["robots"]
        info, urls = None, []
        if robots is None:
            robots = await self.robots(host)
            rules = parse_robots(robots)
            delay = float(rules.crawl_delay(UA) or 1.0)
            info = {"robots": robots, "crawl_delay": delay}
        else:
            rules = parse_robots(robots)
        if h["want_sitemaps"]:
            urls = await self.sitemaps(rules.site_maps() or [], delay)
            info = {"crawl_delay": delay, **(info or {}), "sitemaps": True}
        pages = []
        for u in h["urls"]:
            if not rules.can_fetch(UA, u["url"]):
                pages.append({"url": u["url"], "depth": u["depth"], "status": ROBOTS_BLOCKED})
                continue
            pages.append(await self.fetch_page(u))
            await asyncio.sleep(delay)
        return {"host": host, "pages": pages, "info": info, "urls": urls}

    async def robots(self, host: str) -> str:
        for scheme in ("https", "http"):
            body, status = await self.fetch_bytes(f"{scheme}://{host}/robots.txt")
            if status == 200:
                return body.decode(errors="replace")
            if status:
                break
        return ""

    async def sitemaps(self, sitemaps: list[str], delay: float) -> list[str]:
        found: list[str] = []
        for sm in sitemaps[:MAX_SITEMAPS]:
            body, _ = await self.fetch_bytes(sm)
            await asyncio.sleep(delay)
            locs = LOC.findall(body.decode(errors="replace"))
            if b"<sitemapindex" in body:
                for child in locs[:MAX_SITEMAPS]:
                    body, _ = await self.fetch_bytes(child)
                    await asyncio.sleep(delay)
                    found += LOC.findall(body.decode(errors="replace"))
            else:
                found += locs
            if len(found) >= MAX_SITEMAP_URLS:
                break
        return sorted({n for n in map(normalize, found) if n})[:MAX_SITEMAP_URLS]

    async def fetch_bytes(self, url: str) -> tuple[bytes, int]:
        """Small helper for robots and sitemaps. Returns (body, status); status 0 on error."""
        try:
            r = await self.http.get(url)
            self.stats.requests += 1
            self.stats.bytes += len(r.content)
            body = r.content
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body, r.status_code
        except Exception:
            self.stats.error("other")
            return b"", 0

    async def fetch_page(self, u: dict[str, Any]) -> dict[str, Any]:
        url = u["url"]
        headers = {}
        if u.get("etag"):
            headers["If-None-Match"] = u["etag"]
        if u.get("last_modified"):
            headers["If-Modified-Since"] = u["last_modified"]
        result: dict[str, Any] = {"url": url, "depth": u["depth"], "status": 0}
        self.stats.pages += 1
        try:
            async with self.http.stream("GET", url, headers=headers) as r:
                self.stats.requests += 1
                result["status"] = r.status_code
                if r.status_code != 200:
                    if r.status_code != 304:
                        self.stats.error(f"http_{r.status_code // 100}xx")
                    return result
                if "text/html" not in r.headers.get("content-type", ""):
                    result["status"] = 415
                    self.stats.error("not_html")
                    return result
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        result["status"] = 413
                        self.stats.error("too_large")
                        return result
                self.stats.bytes += len(body)
                final = normalize(str(r.url))
                if final and final != url:
                    result["final_url"] = final
                result["etag"] = r.headers.get("etag")
                result["last_modified"] = r.headers.get("last-modified")
                page = await asyncio.get_running_loop().run_in_executor(
                    EXTRACT, extract, bytes(body), final or url
                )
                if page:
                    result.update(page)
                else:
                    self.stats.error("unparsable")
        except httpx.TimeoutException:
            self.stats.error("timeout")
        except httpx.HTTPError:
            self.stats.error("connect")
        except Exception as e:
            log.warning("%s: %s", url, e)
            self.stats.error("other")
        return result


def parse_robots(text: str) -> RobotFileParser:
    rules = RobotFileParser()
    rules.parse(text.splitlines())
    return rules


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.CRITICAL)
    api = Api(os.environ["BINGUS_API_URL"], os.environ["BINGUS_API_KEY"])
    stats = Stats()

    async def run() -> None:
        log.info("%s: %d hosts in flight, results every %ds", INSTANCE, HOSTS, FLUSH)
        worker = Worker(api, stats)
        await asyncio.gather(worker.run(), worker.flush(), stats.run(api))

    asyncio.run(run())
