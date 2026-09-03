import hashlib
import re
from collections import Counter

import xxhash

WORD = re.compile(r"\w+")


def signed64(value: int) -> int:
    """Fit an unsigned 64-bit hash into a Postgres bigint."""
    return value - (1 << 64) if value >= (1 << 63) else value


def url_hash(url: str) -> int:
    return signed64(xxhash.xxh3_64_intdigest(url.encode()))


def content_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode()).digest()


def simhash(text: str) -> int:
    """64-bit simhash over lowercase words. Similar texts differ in few bits."""
    words: list[str] = WORD.findall(text.lower())
    acc = [0] * 64
    for word, n in Counter(words).items():
        h = xxhash.xxh3_64_intdigest(word.encode())
        for i in range(64):
            acc[i] += n if h >> i & 1 else -n
    return signed64(sum(1 << i for i in range(64) if acc[i] > 0))


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()
