FROM node:20-alpine AS frontend-build
WORKDIR /workspace
COPY package.json ./
COPY scripts ./scripts
COPY frontend ./frontend
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MERIDIAN_HOST=0.0.0.0 \
    MERIDIAN_PORT=5001
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential unixodbc-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend ./backend
COPY app.py ./
COPY --from=frontend-build /workspace/frontend/dist ./frontend
RUN mkdir -p storage/uploads storage/exports storage/knowledge storage/workspaces storage/trash
EXPOSE 5001
CMD ["python", "app.py"]

