# Chronos Trading Agent - Dockerfile
# Python 3.11 slim for smaller image size

FROM python:3.11-slim

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

# Copy application code
COPY . .

# Create data directories
RUN mkdir -p /app/data/trade_logs /app/model_checkpoints

# Set Python to run unbuffered (better for logging)
ENV PYTHONUNBUFFERED=1

# Default command: run the trading agent
CMD ["python", "main.py"]

