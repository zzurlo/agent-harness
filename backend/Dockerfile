FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "openai>=1.54" \
    "azure-identity>=1.19" "azure-data-tables>=12.5" "httpx>=0.27" \
    "pydantic>=2.9" "tiktoken>=0.8"

COPY harness ./harness
COPY api ./api

EXPOSE 8000

# Single worker: Container Apps scales replicas, not processes. Keeps the
# per-replica memory footprint small enough for 0.5 vCPU / 1 GiB.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
