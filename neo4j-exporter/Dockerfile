FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534
WORKDIR /app
RUN pip install --no-cache-dir neo4j>=5.0.0
COPY exporter.py .
CMD ["python", "exporter.py"]
