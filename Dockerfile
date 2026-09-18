FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Gibraltar

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY toppswatch/ ./toppswatch/

RUN useradd -m -u 1000 watcher && mkdir -p /app/data && chown -R watcher /app
USER watcher

VOLUME ["/app/data"]

HEALTHCHECK --interval=5m --timeout=20s --start-period=90s --retries=3 \
  CMD python -c "import json,time,sys; \
s=json.load(open('/app/data/state.json')); \
sys.exit(0 if time.time()-s['saved_at'] < 900 else 1)"

ENTRYPOINT ["python", "-m", "toppswatch.main", "--config", "/app/config.yaml"]
CMD ["run"]
