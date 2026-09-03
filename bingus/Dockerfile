FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# api, fetch ou embed. Cada imagem instala sÃ³ o que o seu modo usa.
ARG EXTRA=api
ENV UV_COMPILE_BYTECODE=1 PATH=/app/.venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra $EXTRA
COPY bingus ./bingus
RUN uv sync --frozen --no-dev --extra $EXTRA

# torch 2.14 manda até matmul para kernels Triton em eager, que exigem gcc e não rodam em Pascal.
ENV TORCH_DISABLE_NATIVE_JIT=1

CMD ["bingus-api"]
