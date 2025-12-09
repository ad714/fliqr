# Use small official image
FROM python:3.10-slim

# Create non-root user for safety
ENV APP_USER=appuser
RUN adduser --disabled-password --gecos "" $APP_USER

WORKDIR /app

# Install system deps needed by some packages (kept minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage layer cache
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy application
COPY . /app

# Ensure non-root owns files and then switch to non-root
RUN chown -R $APP_USER:$APP_USER /app
USER $APP_USER

# Unbuffered output so logs stream
ENV PYTHONUNBUFFERED=1

# Default command
CMD ["python", "fliq_match_result_watcher.py"]
