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
RUN pip install --no-cache-dir -e ".[dev]" || pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Copy frontend build from stage 1 (Vite outputs to weave_ui/static/)
COPY --from=frontend-build /app/weave_ui/static ./weave_ui/static

EXPOSE 8080
CMD ["python", "main.py", "viz", "--host", "0.0.0.0", "--port", "8080"]
