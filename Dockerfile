FROM python:3.12-slim

RUN useradd --create-home appuser
WORKDIR /app

COPY pyproject.toml README.md ./
COPY loxo_llm_router ./loxo_llm_router
RUN pip install --no-cache-dir .

EXPOSE 9090
ENV HOST=0.0.0.0 \
    PORT=9090 \
    TZ=UTC

# Reads $PORT rather than a literal: PORT is overridable at run time, and a
# hardcoded probe port would report the container unhealthy forever whenever it
# is overridden. /health is deliberately unauthenticated, so this also holds on
# deployments that set ROUTER_TOKEN.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c 'import os,urllib.request; urllib.request.urlopen("http://localhost:"+os.environ["PORT"]+"/health", timeout=4)' || exit 1

# The spend ledger lands under this user's home, not /app.
USER appuser
CMD ["loxo-llm-router"]
