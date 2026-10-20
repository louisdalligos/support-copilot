# The approval app: the agent, its MCP servers (in-process) and the review pages.
#
#   docker build -t support-copilot .
#   docker build --build-arg RAG_MODELS=true -t support-copilot:rag .   # + EmbeddingGemma and the reranker
#
# Runs as a non-root user. Health check: GET /healthz.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 copilot

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml README.md ./
RUN mkdir -p src/copilot && touch src/copilot/__init__.py && pip install -e .

ARG RAG_MODELS=false
RUN if [ "$RAG_MODELS" = "true" ]; then \
      pip install torch --index-url https://download.pytorch.org/whl/cpu && pip install -e ".[rag]"; \
    fi

COPY src ./src
COPY data ./data

USER copilot
ENV COPILOT_HOST=0.0.0.0 \
    COPILOT_PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status != 200)"

CMD ["python", "-m", "copilot.approvals"]
