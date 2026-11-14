# Build context is the repository root so the existing `app/` package (ingestion, chunking, providers) is reused.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY backend/ ./
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh \
 && useradd -m app && mkdir -p /data/uploads && chown -R app /data /srv
USER app
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
