FROM python:3.11-slim

# Set by the build workflow from the git tag, so the running container can say
# which release it is. Defaults to "dev" for a local build.
ARG MOTORSPORTARR_VERSION=dev
ARG MOTOGPARR_VERSION=

LABEL maintainer="Motorsportarr"
LABEL description="Motorsport Automated Recording & Retrieval"
LABEL org.opencontainers.image.title="msportarr"
LABEL org.opencontainers.image.description="Motorsport automated retrieval and Plex library manager"
LABEL org.opencontainers.image.source="https://github.com/dougalbob/msportarr"
LABEL org.opencontainers.image.version="${MOTORSPORTARR_VERSION}"

# fonts-dejavu-core: text rendering for the artwork overlay.
# util-linux: supplies setpriv, which entrypoint.sh uses to drop root to
#             PUID:PGID at runtime. A Dockerfile USER would be simpler but would
#             bake in one uid, and on Unraid the right uid is per-installation.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY utils/ ./utils/
COPY series/ ./series/
COPY config/ ./config/
COPY assets/ ./assets/
COPY motogp_common.py .
COPY motogp_grabber.py .
COPY motogp_sort.py .
COPY motogp_scheduler.py .
COPY motogp_watcher.py .
COPY motogp_discord.py .
COPY motogp_plex_tagger.py .
COPY motogp_overlay_utility.py .
COPY motogp_webui.py .
COPY grab_stats.py .
COPY activity_emitter.py .
COPY templates/ ./templates/
COPY static/ ./static/
COPY entrypoint.sh .

RUN chmod +x entrypoint.sh *.py

ENV PYTHONUNBUFFERED=1
ENV CONFIG_DIR=/config
ENV TZ=Europe/London
ENV MOTORSPORTARR_VERSION=${MOTORSPORTARR_VERSION}
ENV MOTOGPARR_VERSION=${MOTOGPARR_VERSION}

VOLUME ["/config"]

EXPOSE 5679

# The WebUI can die while the scheduler keeps the container "Up". Without this,
# Docker (and therefore Unraid) reports healthy for a container whose UI is gone,
# which is exactly the confusion this probe removes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["/opt/venv/bin/python3", "/app/utils/healthcheck.py"]

ENTRYPOINT ["/app/entrypoint.sh"]