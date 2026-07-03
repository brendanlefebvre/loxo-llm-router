FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY loxo_llm_router ./loxo_llm_router
RUN pip install --no-cache-dir .

EXPOSE 9090
ENV HOST=0.0.0.0 \
    PORT=9090 \
    TZ=UTC

CMD ["loxo-llm-router"]
