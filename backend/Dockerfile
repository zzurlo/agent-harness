FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY harness ./harness
COPY api ./api
COPY scripts ./scripts
# Keep the image and local/CI dependencies sourced from the same manifest.
RUN pip install --no-cache-dir .

EXPOSE 8000

# Single worker: Container Apps scales replicas, not processes. Keeps the
# per-replica memory footprint small enough for 0.5 vCPU / 1 GiB.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
