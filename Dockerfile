FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 zlb \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin zlb
LABEL org.opencontainers.image.source="https://github.com/abexamir/zlb" \
      org.opencontainers.image.description="Kubernetes LoadBalancer controller for an external Linux forwarding node" \
      org.opencontainers.image.licenses="Apache-2.0"
WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY zlb ./zlb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "-m", "zlb.controller"]
