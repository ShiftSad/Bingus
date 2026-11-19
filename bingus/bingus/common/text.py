# Cerca de 512 tokens de português, o contexto do embed. Medido em 2765 páginas e 630 queries:
# chunk de 1700 com 512 tokens ganha do de 7000 com 2048 em trecho do meio/fim (MRR@10 0,73 contra
# 0,59), empata no título e embeda 26% mais páginas por segundo. Truncar o de 7000 em 512 perdia
# o que vem depois do começo. Custo: uns 3,2 chunks por página em vez de 1,3.
CHUNK_CHARS = 1700


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
