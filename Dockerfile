# --- Stage 1: Build dependencies ---
FROM python:3.12-slim AS builder

WORKDIR /build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build dependencies (e.g. compiler for native libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy setup file to resolve requirements
COPY setup.py /build/

# Build wheels for dependency caching
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /build/wheels -e .

# --- Stage 2: Runtime image ---
FROM python:3.12-slim AS runner

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/home/trader/.local/bin:$PATH"

# Create a non-privileged system user/group
RUN groupadd -g 10001 trader && \
    useradd -u 10001 -g trader -m -s /bin/bash trader

# Copy wheels from builder and install them
COPY --from=builder /build/wheels /app/wheels
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --no-index --find-links=/app/wheels /app/wheels/* && \
    rm -rf /app/wheels

# Copy application files and change ownership to the non-privileged user
COPY --chown=trader:trader . /app/

# Install the local application package
RUN pip install --no-cache-dir .

# Switch to the non-privileged user
USER trader

# Expose metrics port
EXPOSE 8000

# Container healthcheck querying the Prometheus metrics endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/metrics', timeout=2)" || exit 1

# Run the strategy
ENTRYPOINT ["python", "main.py"]
