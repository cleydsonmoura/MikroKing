FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependências da aplicação + servidor WSGI de produção
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Código da aplicação
COPY app.py config.py ./
COPY templates ./templates

# Usuário sem privilégios
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5000

# 1 worker: o coletor roda em uma thread em segundo plano.
# --threads permite atender requisições HTTP concorrentes.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]