FROM python:3.12-alpine
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Copia i file delle dipendenze
COPY pyproject.toml uv.lock ./

# Installa le dipendenze
RUN uv sync --frozen --no-cache

# Copia il resto dell'applicazione
COPY . .

ENV PYTHONUNBUFFERED=1

# Esegue tramite uv run per gestire l'ambiente correttamente
ENTRYPOINT ["uv", "run", "main.py", "-c", "/exporter_config.yaml"]