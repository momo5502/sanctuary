FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CODEX_VERSION=latest
RUN npm install --global "@openai/codex@${CODEX_VERSION}"

ENV PYTHONPATH=/opt/sanctuary
ENV SANCTUARY_HARNESS=codex
ENV SANCTUARY_WORKDIR=/workspace
WORKDIR /workspace
COPY sanctuary /opt/sanctuary/sanctuary

ENTRYPOINT ["python3", "-m", "sanctuary.runtime"]
