FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DRIVEHUB_DATA=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY drivehub ./drivehub
RUN pip install --no-cache-dir . \
 && useradd --create-home --uid 1000 drivehub \
 && mkdir -p /data && chown drivehub /data
USER drivehub
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["drivehub", "serve", "--host", "0.0.0.0", "--port", "8080"]
