FROM alpine:latest@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b

# redis-cli (FalkorDB is redis-protocol-compatible) + bash + curl for the mc
# download below. mc (MinIO client) isn't packaged for Alpine, so it's pulled
# as a static binary at build time, same pattern as pulling a base image.
RUN apk add --no-cache bash redis curl \
    && curl -fsSL -o /usr/local/bin/mc https://dl.min.io/client/mc/release/linux-amd64/mc \
    && chmod +x /usr/local/bin/mc

COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh

ENTRYPOINT ["/usr/local/bin/backup.sh"]
