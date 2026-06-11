# Weave UI — Multi-Task Orchestration Dashboard

## Overview

Weave UI is a web dashboard for the Weave multi-agent orchestration system. It provides a visual interface for task submission, real-time monitoring, execution debugging, and history management.

## Architecture

```
Browser (Vue 3 + Naive UI)
    |  /api/*  (REST)    /ws (WebSocket)
    v
FastAPI Server (weave_ui/server.py)
    |
    +-- JobRepository (JSON files in data/jobs/)
    +-- ApprovalRepository (JSON files)
    +-- SessionStore (JSONL event logs)
    +-- Static Files (weave_ui/static/ from Vite build)
```

- **Single process**: FastAPI serves both the API and the compiled frontend
- **Single container**: One Docker image with multi-stage build
- **Real-time**: WebSocket pushes execution events to the dashboard

## Quick Start

### Development Mode

1. Start the backend:
   ```bash
   python main.py viz
   ```

2. In another terminal, start the frontend dev server:
   ```bash
   cd weave_ui/frontend && npm install && npm run dev
   ```

3. Open http://localhost:3000 (Vite proxies API calls to :8080)

### Production Mode

```bash
cd weave_ui/frontend && npm install && npm run build
python main.py viz
# Open http://localhost:8080
```

### Docker

```bash
docker compose up -d
# Open http://localhost:8080
```

## Features

| Feature | Description | Phase |
|---------|------------|-------|
| Task Submission | Submit tasks with workspace + prompt + template | M8.2 |
| Dashboard | Real-time job cards grouped by status | M8.2 |
| Job Detail | Event timeline + actions (cancel/retry/approve) | M8.2 |
| Browser Notifications | Push alerts for status changes | M8.3 |
| History Search | Full-text search with filters | M8.3 |
| Template Library | CRUD for task templates with variables | M8.4 |
| Annotations | Tags + notes + star rating per job | M8.4 |
| Execution Replay | Step-by-step event replay | M8.4 |

## API Endpoints

### Jobs
- `GET /api/jobs` — List jobs (optional `?status=` filter)
- `GET /api/jobs/{id}` — Job details with runs
- `POST /api/jobs` — Submit new job `{requirement, workspace, template?}`
- `POST /api/jobs/{id}/cancel` — Cancel a job
- `POST /api/jobs/{id}/retry` — Retry a failed job
- `GET /api/jobs/{id}/summary` — Task completion summary
- `GET /api/jobs/{id}/annotations` — Get annotations
- `PUT /api/jobs/{id}/annotations` — Update annotations `{tags?, notes?, rating?}`

### Search & Notifications
- `GET /api/search?q=&status=&from=&to=` — Full-text search
- `GET/PUT /api/notification-preferences` — Browser notification settings

### Templates
- `GET/POST /api/task-templates` — List/create task templates
- `PUT/DELETE /api/task-templates/{name}` — Update/delete

### Other
- `GET /api/sessions` — List sessions
- `GET /api/sessions/{id}` — Session events + DAG
- `GET /api/tickets` — List approval tickets
- `POST /api/tickets/{id}/approve` — Approve ticket
- `GET /api/templates` — List DAG templates
- `GET /api/workspaces` — List workspaces
- `GET /api/metrics` — System metrics
- `GET /api/health` — Health check
- `WS /ws` — Real-time event stream

## Configuration

| Env Var | Description | Default |
|---------|-------------|---------|
| `WEAVE_API_KEY` | API key for authentication (optional) | none |

## Frontend Development

The frontend is located at `weave_ui/frontend/` and uses:
- **Vue 3** + **TypeScript** + **Vite**
- **Naive UI** component library
- **Pinia** state management
- **Vue Router** with lazy-loaded views

Key directories:
- `src/views/` — Page components
- `src/components/` — Reusable components
- `src/stores/` — Pinia stores
- `src/composables/` — Vue composables
- `src/api/` — API call layer
- `src/types/` — TypeScript interfaces

Build output goes to `weave_ui/static/` which FastAPI serves as static files.
