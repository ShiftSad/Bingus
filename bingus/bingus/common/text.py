CHUNK_CHARS = 7000  # cerca de 2048 tokens de português


def chunks(text: str) -> list[tuple[int, int]]:
    """Char offsets (start, end) of each chunk, cut at a paragraph or sentence when possible."""
    out: list[tuple[int, int]] = []
    start = 0
    while len(text) - start > CHUNK_CHARS:
        end = start + CHUNK_CHARS
        floor = start + CHUNK_CHARS // 2
        cut = max(text.rfind("\n", floor, end), text.rfind(". ", floor, end))
        if cut > 0:
            end = cut + 1
        out.append((start, end))
        start = end
    out.append((start, len(text)))
    return out
