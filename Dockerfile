# syntax=docker/dockerfile:1
# restic comes from its official image; pinned, and bumped by PR like every other image.
FROM restic/restic:0.19.1 AS restic

FROM python:3.13.18-slim-trixie
# util-linux provides nsenter for post-restore host commands (run from a short-lived helper container).
RUN apt-get update \
 && apt-get install -y --no-install-recommends util-linux ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=restic /usr/bin/restic /usr/local/bin/restic
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# Runs as root on purpose: it restores files with their original owners and talks to the Docker socket.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["backtrack"]
CMD ["serve"]
