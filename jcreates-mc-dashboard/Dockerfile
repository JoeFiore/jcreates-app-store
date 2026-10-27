FROM python:3.12-slim

# Java 21 covers modern Minecraft servers (Paper, Fabric, Velocity).
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
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
