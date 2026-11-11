# ==============================================================================
# iaparacasa - Dockerfile Ultra-Otimizado para Edge AI (Mini PC / VPS Leve)
# Base: Debian Slim com suporte otimizado a llama-cpp-python (CPU AVX2 / Wheels)
# ==============================================================================

FROM python:3.11-slim AS builder

WORKDIR /build

# Dependências mínimas de compilação para extensões C/C++ (llama.cpp) caso precise compilar
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    curl \
    libopenblas-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# Tenta baixar wheel CPU pré-compilado oficial (muito mais rápido); se não houver para a arquitetura, compila com OpenBLAS
ENV CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS"
RUN pip install --no-cache-dir --user --prefer-binary \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu \
    -r requirements.txt

# ------------------------------------------------------------------------------
# Imagem Final de Execução (Runtime enxuto e seguro)
# ------------------------------------------------------------------------------
FROM python:3.11-slim AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libopenblas0 \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Copia pacotes python instalados do estágio de compilação
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

# Criação de diretórios para modelos e dados locais
RUN mkdir -p /app/models /app/data

# Copia código-fonte, testes, prompt e recursos
COPY system_prompt.txt /app/system_prompt.txt
COPY .env.example /app/.env.example
COPY app /app/app
COPY scripts /app/scripts
COPY tests /app/tests

# Verificação de saúde embutida para Docker/Kubernetes
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
