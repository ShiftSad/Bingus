from typing import Any

import py3langid
import trafilatura

from bingus.common.hashing import simhash
from bingus.common.text import chunks
from bingus.common.urls import host_of, normalize

MAX_LINKS = 500
MIN_TEXT = 200  # menos que isso não é artigo, é um hub: só os links interessam


def extract(html: bytes, url: str) -> dict[str, Any] | None:
    """Title, text, language, chunks and links of a page. None when the HTML is unparsable.

    Pages without real text come back with text None but still carry their links.
    """
    tree = trafilatura.load_html(html)
    if tree is None:
        return None
    tree.make_links_absolute(url)
    links = dict.fromkeys(
        normalize(str(href))
        for el, attr, href, _ in tree.iterlinks()
        if el.tag == "a" and attr == "href"
    )
    links.pop(None, None)
    links.pop(url, None)
    canonical = tree.xpath('string(//link[@rel="canonical"]/@href)')

    doc = trafilatura.bare_extraction(
        tree,
        url=url,
        with_metadata=True,
        include_comments=False,
        favor_precision=True,
        as_dict=True,
    )
    title = (doc.get("title") or "").strip() if isinstance(doc, dict) else ""
    body = (doc.get("text") or "") if isinstance(doc, dict) else ""
    if len(body) >= MIN_TEXT:
        text = f"{title}\n\n{body}" if title else body
        sample = text
    else:
        text = None
        sample = " ".join(tree.xpath("//body//text()[not(ancestor::script or ancestor::style)]"))
    lang, _ = py3langid.classify(sample[:5000])

    out: dict[str, Any] = {
        "title": title or None,
        "text": text,
        "lang": lang,
        "published": doc.get("date") if isinstance(doc, dict) else None,
        "chunks": [{"start": s, "end": e, "simhash": simhash(text[s:e])} for s, e in chunks(text)]
        if text
        else [],
        "links": [u for u in links if u][:MAX_LINKS],
    }
    canon = normalize(canonical, url) if canonical else None
    if canon and canon != url and host_of(canon) == host_of(url):
        out["final_url"] = canon
    return out
