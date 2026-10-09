FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY refused ./refused
ENV LISTEN_PORT=8080 DATA_DIR=/data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/refused/healthz'%os.environ.get('LISTEN_PORT','8080'),timeout=4)" || exit 1
# One process only: settings, session and poster state live in memory.
CMD ["sh", "-c", "exec uvicorn refused.main:create_app --factory --host 0.0.0.0 --port ${LISTEN_PORT} --no-access-log"]
