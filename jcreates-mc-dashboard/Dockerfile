FROM python:3.12-slim-trixie

# Java 25: this server's Minecraft bundler is compiled to class file
# version 69.0 (Java 25), and Java 21 only reads up to 65.0, so a
# Java 21 runtime dies with UnsupportedClassVersionError on launch.
# Debian 13 (trixie) is pinned here on purpose: openjdk-25-jre-headless
# is in trixie's main archive, and it is not in bookworm's.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-25-jre-headless \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY app/backend ./backend
COPY app/frontend ./frontend
COPY app/entrypoint.sh ./entrypoint.sh
COPY app/server-readme.txt ./server-readme.txt

RUN pip install --no-cache-dir -r backend/requirements.txt \
    && chmod +x entrypoint.sh

EXPOSE 8000 25565

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/')"

ENTRYPOINT ["./entrypoint.sh"]
