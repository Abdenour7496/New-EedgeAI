FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir 'hermes-agent[web,pty]==0.16.0'

EXPOSE 9119

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:9119/ || exit 1

CMD ["hermes", "dashboard", "--host", "0.0.0.0", "--port", "9119", "--insecure", "--no-open"]
