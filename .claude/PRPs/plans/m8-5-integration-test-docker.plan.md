# Plan: M8.5 — 集成测试 + Docker 部署 + 用户文档

## Summary
端到端验证 weave-ui 完整功能，配置 Docker 一键部署，编写用户文档。

## Metadata
- Complexity: Large
- Source Issue: #1102 (Parent: #1097)
- Depends on: M8.2, M8.3, M8.4
- Estimated Files: 12

## Files to Change

### 测试 (3 created)
- tests/test_weave_ui_api.py — CREATE: API 集成测试 (pytest + httpx)
- tests/test_weave_ui_e2e.py — CREATE: E2E 流程测试 (Playwright 可选)
- weave_ui/frontend/tests/ — CREATE: 前端组件测试 (Vitest 可选)

### Docker (3 created/modified)
- Dockerfile — UPDATE: 多阶段构建 (npm build + Python runtime)
- docker-compose.yml — UPDATE: 添加 weave-ui service
- docker-compose.dev.yml — CREATE: 开发模式 (热重载)

### 构建 (1 created)
- Makefile 或 Justfile — CREATE: ui-dev, ui-build, ui-start 快捷命令

### 文档 (3 created, 1 modified)
- docs/weave-ui.md — CREATE: 功能介绍 + 架构图 + 使用指南
- CLAUDE.md — UPDATE: 添加 weave_ui/ 模块说明
- README section — UPDATE: 添加 Weave UI 快速开始

### 配置 (1 modified)
- .gitignore — UPDATE: 确保排除 static/ 构建产物

## Step-by-Step Tasks

### Task 1: API 集成测试
- tests/test_weave_ui_api.py: 覆盖所有 API endpoint
- 核心: POST /api/jobs, GET /api/jobs, WebSocket events, tickets
- 工具: pytest + httpx.AsyncClient
- VALIDATE: pytest tests/test_weave_ui_api.py

### Task 2: E2E 流程测试
- 测试环境: 本地启动 FastAPI + 构建前端
- 核心路径: 提交 -> 监控 -> 详情 -> 审批 -> 搜索
- VALIDATE: pytest tests/test_weave_ui_e2e.py

### Task 3: Docker 多阶段构建
- Stage 1: node -> npm run build -> weave_ui/static/
- Stage 2: python -> uvicorn + static files
- docker-compose: weave-ui service + 共享数据卷
- docker-compose.dev: 热重载开发模式
- VALIDATE: docker compose build

### Task 4: Makefile/Justfile
- ui-dev: 启动前后端开发模式
- ui-build: 构建前端
- ui-start: 生产模式启动
- VALIDATE: make ui-build

### Task 5: 用户文档
- docs/weave-ui.md: 功能介绍 + 架构图
- 快速开始: git clone -> pip install -> npm install -> npm run build -> python main.py viz
- Docker 部署: docker compose up -d
- 配置说明: API Key, Workspace, 通知
- VALIDATE: 文档可读性

### Task 6: CLAUDE.md 更新
- weave_ui/ 模块说明
- 前端开发规范: 组件命名, composable, API 调用
- 后端 API 开发规范: server.py 代码风格
- VALIDATE: 文档完整性

## Validation
- python -m pytest tests/test_weave_ui_*.py -x -q
- cd weave_ui/frontend && npm run build
- docker compose build (如环境支持)
- 新用户按文档 15 分钟内部署完成

## NOT Building
- 截图/GIF (需手动截图)
- CI/CD pipeline 配置
