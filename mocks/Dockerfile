FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Non-root user pinned at the image level per docs/specs/16.
# uid/gid mirror the spec's 10001 convention so this mock matches the
# real deployment shape.
RUN groupadd --system --gid 10001 miployees \
 && useradd  --system --uid 10001 --gid miployees --home-dir /app --shell /usr/sbin/nologin miployees

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.30" \
    "jinja2>=3.1" \
    "itsdangerous>=2.2" \
    "python-multipart>=0.0.9"

COPY --chown=miployees:miployees app /app/app

USER miployees:miployees

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

# Hot reload (three paths, all confirmed):
#   .py           → uvicorn --reload via WatchFiles, scoped to /app/app
#   .html (Jinja) → Starlette's Jinja2Templates env defaults to auto_reload=True
#   /static/*     → FastAPI StaticFiles reads from disk per request
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--reload", "--reload-dir", "/app/app"]
