# Vidette — multi-stage build: web shell → single slim Python runtime image (non-root).

# --- web build -------------------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /build
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY web/ .
RUN npm run build

# --- runtime ---------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

RUN useradd --system --uid 1000 --create-home vidette \
    && mkdir -p /config /media/vidette \
    && chown -R vidette:vidette /config /media/vidette

WORKDIR /app
COPY server/ /app/server/
RUN pip install --no-cache-dir /app/server

COPY --from=web /build/dist /app/web-dist
ENV VIDETTE_WEB_DIST=/app/web-dist \
    PYTHONUNBUFFERED=1

USER vidette
EXPOSE 8642
VOLUME ["/config", "/media/vidette"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8642/healthz', timeout=3)"

CMD ["vidette", "serve"]
