# stopguard + offizielle Scalable CLI in einem Container (für Server/NAS/Raspberry Pi 64-bit).
#
# Die offiziellen Linux-Binaries der Scalable CLI brauchen glibc >= 2.38 –
# deshalb Debian 13 (trixie) als Basis (Debian 12 / Raspberry Pi OS Bookworm reichen NICHT).
FROM python:3.13-slim-trixie

ARG SC_VERSION=v1.3.0
# Öffentlicher Release-Signaturschlüssel von Scalable Capital (public-docs/verify-releases.md)
ARG SC_MINISIGN_KEY=RWRKuuSASIzbSYpuU5gdXeTkXirJBl5+XVXLP6E60hBUUKZ5HPIGjV8b
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl minisign tzdata \
 && rm -rf /var/lib/apt/lists/*

# Scalable CLI herunterladen und Herkunft prüfen (minisign-Signatur + SHA-256)
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=aarch64 ;; \
      *) echo "Nicht unterstützte Architektur: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/ScalableCapital/scalable-cli/releases/download/${SC_VERSION}"; \
    asset="sc-${SC_VERSION}-linux-${arch}-gnu.tar.gz"; \
    sums="sc-${SC_VERSION}-SHA256SUMS"; \
    cd /tmp; \
    curl -fsSLO "${base}/${asset}"; \
    curl -fsSLO "${base}/${sums}"; \
    curl -fsSLO "${base}/${sums}.minisig"; \
    minisign -V -P "${SC_MINISIGN_KEY}" -m "${sums}" -x "${sums}.minisig"; \
    grep -F "  ${asset}" "${sums}" | sha256sum -c -; \
    mkdir sc && tar -xzf "${asset}" -C sc; \
    install -m 0755 "$(find sc -type f -name sc | head -n1)" /usr/local/bin/sc; \
    rm -rf /tmp/*; \
    sc --version

RUN useradd --create-home --shell /usr/sbin/nologin stopguard
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /app/src

USER stopguard
# Im Container gibt es keinen System-Schlüsselbund → dateibasierte Session der Scalable CLI.
# Das Home-Verzeichnis wird als Volume persistiert (Session, Zustand, Protokoll).
RUN mkdir -p /home/stopguard/.config/scalable-cli /home/stopguard/.local/state/stopguard \
 && printf '[auth]\nsession_backend = "file"\n' > /home/stopguard/.config/scalable-cli/config.toml \
 && chmod 700 /home/stopguard/.config/scalable-cli
VOLUME ["/home/stopguard"]

ENV TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1 \
    STOPGUARD_CONFIG=/config/stopguard.toml

HEALTHCHECK --interval=30m --timeout=30s CMD python -c "import json,os,sys,datetime as d;p=os.path.expanduser('~/.local/state/stopguard/state.json');s=json.load(open(p));t=d.datetime.fromisoformat(s['last_run_at']);sys.exit(0 if (d.datetime.now(d.timezone.utc)-t).total_seconds()<86400*3 else 1)" || exit 1

ENTRYPOINT ["stopguard"]
CMD ["daemon"]
