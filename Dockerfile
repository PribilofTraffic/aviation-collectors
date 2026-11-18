FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so a code change does not reinstall them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AVIATION_DATA_DIR=/data \
    AVIATION_CONFIG_DIR=/config

# Data and config are mounted from the host at run time
RUN mkdir -p /data /config && chmod 777 /data

# Healthy = the main loop is alive (see app/healthcheck.py)
HEALTHCHECK --interval=1m --timeout=10s --start-period=1m --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

CMD ["python", "/app/run.py"]
