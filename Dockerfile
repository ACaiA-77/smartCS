FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/home/app/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/home/app/.cache/sentence-transformers \
    FAISS_INDEX_PATH=/app/vector_store/faiss_index \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

RUN adduser --disabled-password --gecos "" app \
    && mkdir -p /app/data /app/vector_store /app/knowledge_base /app/knowledge_sources "$HF_HOME" "$SENTENCE_TRANSFORMERS_HOME" \
    && chown -R app:app /app /home/app

COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.13.0+cpu
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --prefer-binary \
    --index-url "${TORCH_INDEX_URL}" --extra-index-url "${PIP_INDEX_URL}" \
    "torch==${TORCH_VERSION}" \
    && python -m pip install --no-cache-dir --prefer-binary --retries 5 --timeout 120 \
    --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY --chown=app:app . .

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=5)"

CMD ["sh", "-c", "uvicorn api.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000}"]
