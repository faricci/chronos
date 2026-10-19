# Chronos v2 Trading Agent - Dockerfile
# Python 3.12 slim for smaller image size

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for ML libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (better layer caching)
COPY requirements.txt .

# Install Python dependencies
# Use CPU-only PyTorch to avoid huge CUDA downloads
# Use --no-cache-dir to reduce image size
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

# Copy ONLY the application code. Never "COPY . .": the build context holds the .env with the API key, the
# multi-GB bar history (data/) and the models, which are mounted as volumes at run time (docker-compose.yml).
COPY modules/ ./modules/
COPY config/config.example.yml ./config/config.example.yml

# Mount points: data/ and models/ are bind mounts, state/ is the named volume chronos-state (SQLite WAL)
RUN mkdir -p /app/data /app/models /app/state

# Set Python to run unbuffered (better for logging)
ENV PYTHONUNBUFFERED=1

# Default command: the v2 orchestrator. It runs DRY unless config AND CHRONOS_LIVE_CONFIRM both say live, and this
# build refuses live (spec sez. 5.1.1). The v1 main.py is disabled and not copied into the image.
CMD ["python", "-m", "modules.app"]
