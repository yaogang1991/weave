# Plan: M8.2 — 核心页面：任务提交 + 监控面板 + 补充 API

## Summary
为 Weave UI 添加 3 个新后端 API，以及完整的前端页面实现和 Pinia 状态管理。

## Metadata
- Complexity: Large
- Source Issue: #1099
- Estimated Files: 18

## Files to Change

### Backend (1 modified)
- weave_ui/server.py — UPDATE: POST /api/jobs, GET/POST /api/workspaces, GET /api/jobs/{id}/summary

### Frontend (14 created, 3 modified)
- src/types/index.ts — CREATE: TypeScript 类型
- src/api/index.ts — CREATE: API 调用封装
- src/stores/job.ts — CREATE
- src/stores/websocket.ts — CREATE
- src/stores/workspace.ts — CREATE
- src/components/StatusTag.vue — CREATE
- src/components/JobCard.vue — CREATE
- src/components/EventTimeline.vue — CREATE
- src/components/TicketList.vue — CREATE
- src/views/DashboardView.vue — UPDATE
- src/views/TasksView.vue — UPDATE
- src/views/JobDetailView.vue — CREATE
- src/router/index.ts — UPDATE

## Step-by-Step Tasks

Task 1: TypeScript 类型定义 (src/types/index.ts)
Task 2: API 调用层 (src/api/index.ts)
Task 3: 补充后端 API (weave_ui/server.py)
Task 4: Pinia Stores (3 files)
Task 5: 基础组件 (StatusTag, JobCard, TicketList)
Task 6: Dashboard 监控页
Task 7: 任务提交页
Task 8: EventTimeline + JobDetail + 路由
Task 9: 集成验证

## NOT Building
- 通知系统 (M8.3)
- 历史归档搜索 (M8.3)
- 模板管理 (M8.4)

## Validation
- cd weave_ui/frontend && npm run build
- python -m pytest tests/test_weave_ui_*.py -x -q
