# costwatch container image. Build context = repo root:
#   docker build -f deploy/Dockerfile -t costwatch:local .
# Runtime composition (secrets, volume, network, digest trigger) lives in homelab-infra
# provisioning/apphost/costwatch/. Credentials arrive as env vars at start; nothing secret
# is baked into the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt deploy/constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY costwatch ./costwatch

# store.py keeps the sqlite DB at <repo>/data/costwatch.db, i.e. /app/data here: mount a volume.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin costwatch \
    && mkdir -p /app/data && chown costwatch:costwatch /app/data
USER costwatch
VOLUME ["/app/data"]

ENV HOST=0.0.0.0 PORT=8770
EXPOSE 8770
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8770/api/history?days=1', timeout=4); sys.exit(0)"

CMD ["python", "-m", "costwatch"]
