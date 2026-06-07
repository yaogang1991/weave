# Stage 1: Build frontend
FROM node:20-alpine AS frontend-build
WORKDIR /app/weave_ui/frontend
COPY weave_ui/frontend/package*.json ./
RUN npm ci
COPY weave_ui/frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim
WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml requirements.txt* ./
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || pip install --no-cache-dir -r requirements.txt 2>/dev/null || true

# Copy application
COPY . .

# Copy frontend build from stage 1
COPY --from=frontend-build /app/weave_ui/frontend/../static ./weave_ui/static

EXPOSE 8080
CMD ["python", "main.py", "viz", "--host", "0.0.0.0", "--port", "8080"]
