FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/models
WORKDIR /srv
# CPU-only PyTorch keeps the image ~2 GB smaller than the default CUDA build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY embedder/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY embedder/server.py .
RUN useradd -m app && mkdir -p /models && chown app /models
USER app
EXPOSE 8100
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
