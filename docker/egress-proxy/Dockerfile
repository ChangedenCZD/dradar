ARG UBUNTU_IMAGE=docker.io/library/ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
FROM ${UBUNTU_IMAGE}

LABEL org.opencontainers.image.title="DRadar Pier egress proxy" \
      org.opencontainers.image.description="Credentialed Squid sidecar for filtered Pier agent egress" \
      org.opencontainers.image.source="https://github.com/codex-radar/dradar" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        apache2-utils \
        ca-certificates \
        squid \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 start-squid.sh /usr/local/bin/start-squid.sh

USER proxy
EXPOSE 8080
CMD ["/usr/local/bin/start-squid.sh"]
