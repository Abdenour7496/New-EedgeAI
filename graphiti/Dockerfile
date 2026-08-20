FROM zepai/graphiti:0.22.0@sha256:76d14f30afc65d2f914637d67d0c0631a7e779e2740be1ae99b9dc0c5876d2da

USER root
RUN uv pip install --python /app/.venv/bin/python "graphiti-core[falkordb]==0.22.0"

COPY app.py /app/falkordb_app.py

CMD ["/app/.venv/bin/uvicorn", "falkordb_app:app", "--host", "0.0.0.0", "--port", "8000"]
