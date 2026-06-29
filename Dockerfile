# syntax=docker/dockerfile:1.7
# Multi-stage build — mirrors ceq-api pattern (gunternowy/ceq-api Dockerfile)

FROM python:3.12-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir --upgrade pip hatchling
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels '.[service]'

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CRM_CHECK_PORT=8090

RUN useradd --create-home --shell /bin/bash --uid 1001 app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels crm-check[service] \
    && rm -rf /wheels

USER app
EXPOSE 8090

# Phase 1d wires the FastAPI service; for Phase 1a we keep the image
# usable as a CLI runner too.
ENTRYPOINT ["python", "-m"]
CMD ["uvicorn", "crm_check.main:app", "--host", "0.0.0.0", "--port", "8090"]
