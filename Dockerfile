FROM node:20-alpine3.22 AS frontend-build
WORKDIR /workspace
COPY package.json ./
COPY scripts ./scripts
COPY frontend ./frontend
RUN npm run build

FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERIDIAN_ENV=production \
    MERIDIAN_HOST=0.0.0.0 \
    MERIDIAN_PORT=5001
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl gnupg unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
      | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
      -o /etc/apt/sources.list.d/microsoft-prod.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system meridian \
    && useradd --system --gid meridian --home-dir /app --shell /usr/sbin/nologin meridian
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY backend ./backend
COPY skills ./skills
COPY deploy/samples ./deploy/samples
COPY app.py ./
COPY scripts/backup.py scripts/restore.py ./scripts/
COPY --from=frontend-build /workspace/frontend/dist ./frontend
RUN mkdir -p storage/uploads storage/exports storage/knowledge storage/workspaces storage/trash \
    && chown -R meridian:meridian /app/storage
USER meridian
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/ready', timeout=3).read()"]
CMD ["python", "app.py"]
