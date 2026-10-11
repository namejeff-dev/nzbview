FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-pinned.txt ./requirements-pinned.txt
RUN pip install --no-cache-dir -r requirements-pinned.txt \
    && useradd --uid 10001 --create-home nzbview \
    && mkdir /data && chown nzbview:nzbview /data
COPY --chown=nzbview:nzbview . .
USER nzbview
ENV SABNZBD_DB=/data/database_collector.db NZBVIEW_COLLECTOR_CONFIG=/app/collector_config.json
EXPOSE 8787
CMD ["python", "run.py", "--host", "0.0.0.0", "--quiet"]
