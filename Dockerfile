FROM python:3.12-slim

WORKDIR /app

# Install deps first — this layer caches and won't rebuild on code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Streamable HTTP listens here. Endpoint: http://<host>:8080/mcp
EXPOSE 8080

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8080 \
    LOG_LEVEL=INFO

# Run server.py directly (not run.py) — in a container, env vars are injected
# by the orchestrator, so .env loading is unnecessary.
#
# Mount the GA4 service-account JSON as a secret and point
# GOOGLE_APPLICATION_CREDENTIALS at it, e.g.:
#   docker run -e AHREFS_API_KEY=... \
#              -e GA4_PROPERTY_ID=... \
#              -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/ga4-sa.json \
#              -v /local/ga4-sa.json:/secrets/ga4-sa.json:ro \
#              -p 8080:8080 seo-mcp
CMD ["python", "server.py"]
