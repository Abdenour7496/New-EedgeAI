FROM python:3.11-slim
WORKDIR /app

# System packages required for OCR and PDF rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-fra \
        tesseract-ocr-deu \
        tesseract-ocr-spa \
        tesseract-ocr-chi-sim \
        poppler-utils \
        ghostscript \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY document_intel.py .
COPY governance.py .
COPY templates/ ./templates/
# Baked in (not just bind-mounted at CI/dev time) so `docker exec
# eedgeai-proxy-1 python3 -m unittest tests.test_ingest -v` — the
# live-stack integration suite, README.md's Testing section — works
# against any real deployment out of the box, not only a checkout with
# the tests/ directory manually copied in after the fact.
COPY tests/ ./tests/
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5001"]
