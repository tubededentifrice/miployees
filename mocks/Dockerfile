# crewday mocks — multi-stage build.
#
# Stage 1 (node:22-alpine) builds the Vite/React SPA under mocks/web.
# Stage 2 (python:3.12-slim) runs FastAPI and serves the built dist/.
# Node is *not* present at runtime; the runtime image stays slim.

# ── Stage 1: build the SPA ────────────────────────────────────────────
FROM node:25-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY web/ ./
RUN npm run build

# ── Stage 2: runtime ─────────────────────────────────────────────────
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Non-root user pinned at the image level per docs/specs/16.
# uid/gid mirror the spec's 10001 convention so this mock matches the
# real deployment shape.
RUN groupadd --system --gid 10001 crewday \
 && useradd  --system --uid 10001 --gid crewday --home-dir /app --shell /usr/sbin/nologin crewday

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "python-multipart>=0.0.9"

COPY --chown=crewday:crewday app /app/app
COPY --from=web-build --chown=crewday:crewday /web/dist /app/web/dist

USER crewday:crewday

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# Hot reload of the Python layer:
#   .py → uvicorn --reload via WatchFiles, scoped to /app/app.
# The SPA is rebuilt with the image (or via the separate web-dev
# service under the `dev` profile) — no Node at runtime.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/app"]
