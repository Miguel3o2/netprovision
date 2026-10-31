FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py models.py transport.py observability.py ./
COPY api ./api
COPY inventory ./inventory
COPY provisioner ./provisioner
COPY web ./web
COPY configs ./configs

RUN useradd --create-home --uid 10001 netprov \
 && mkdir -p /app/data \
 && chown -R netprov:netprov /app

USER netprov

ENV NP_CONFIG=/app/config.yaml \
    NP_DB_PATH=/app/data/inventory.db

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=2)"

CMD ["python", "main.py"]
