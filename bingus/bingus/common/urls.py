import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

SKIP_PARAMS = re.compile(
    r"^(utm_.*|fbclid|gclid|dclid|msclkid|yclid|mc_cid|mc_eid|_ga|_gl|ref|ref_src|igshid"
    + r"|jsessionid|phpsessid|sessionid|sid)$",
    re.I,
)
SESSION_IN_PATH = re.compile(r";jsessionid=[^/?]*", re.I)
SKIP_EXT: tuple[str, ...] = tuple(
    (
        ".png .jpg .jpeg .gif .webp .svg .ico .pdf .zip .rar .gz .7z .mp3 .mp4 .avi .mov .css .js"
        + " .woff .woff2 .ttf .doc .docx .xls .xlsx .ppt .pptx .exe .apk .dmg .iso"
    ).split()
)
MAX_LEN = 2048
# Redes sociais e afins: sem conteúdo útil e infinitos.
BLOCKED_HOSTS = (
    "youtube.com youtu.be instagram.com facebook.com twitter.com x.com linkedin.com tiktok.com"
    + " whatsapp.com apple.com play.google.com web.archive.org t.me pinterest.com spotify.com"
).split()


def normalize(url: str, base: str | None = None) -> str | None:
    """Canonical form of a page URL, or None when it is not worth crawling."""
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        return None

    default_port = 80 if parts.scheme == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"

    path = SESSION_IN_PATH.sub("", parts.path or "/")
    path = quote(unquote(path), safe="/:@!$&'()*+,;=~-._")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path.lower().endswith(SKIP_EXT):
        return None

    params = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not SKIP_PARAMS.match(k)
    )
    query = urlencode(params)

    result = urlunsplit((parts.scheme, netloc, path, query, ""))
    return result if len(result) <= MAX_LEN else None


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""
