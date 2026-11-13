# Use official Python slim image
FROM python:3.11-slim

# Prevent Python from writing pyc files to disk and disable stdout buffering
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HOST=0.0.0.0

WORKDIR /app

# Install curl, ca-certificates, and libatomic1 (required for Playwright's node runner)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium and its required OS-level dependencies
RUN playwright install --with-deps chromium

# Copy application source code
COPY . .

# Expose server port
EXPOSE 8000

# Start server
CMD ["python", "start_server.py"]

