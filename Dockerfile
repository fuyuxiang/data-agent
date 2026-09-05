FROM node:20-alpine AS frontend-build
WORKDIR /workspace
COPY package.json ./
COPY scripts ./scripts
COPY frontend ./frontend
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERIDIAN_ENV=production \
    MERIDIAN_HOST=0.0.0.0 \
    MERIDIAN_PORT=5001
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential unixodbc-dev \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system meridian \
    && useradd --system --gid meridian --home-dir /app --shell /usr/sbin/nologin meridian
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
COPY app.py ./
COPY scripts/backup.py ./scripts/backup.py
COPY --from=frontend-build /workspace/frontend/dist ./frontend
RUN mkdir -p storage/uploads storage/exports storage/knowledge storage/workspaces storage/trash \
    && chown -R meridian:meridian /app/storage
USER meridian
EXPOSE 5001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/api/ready', timeout=3).read()"]
CMD ["python", "app.py"]
