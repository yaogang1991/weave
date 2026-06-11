# Plan: M8.4 — 模板库 + 任务摘要 + 标注备注 + 执行回放

## Summary
实现增强功能：任务模板 CRUD、完成摘要渲染、标注备注、执行历史回放。

## Metadata
- Complexity: Large
- Source Issue: #1101 (Parent: #1097)
- Depends on: M8.2, M8.3
- Estimated Files: 16

## Files to Change

### Backend (1 modified)
- weave_ui/server.py — UPDATE: 添加模板 CRUD API + 标注 API

### 新增数据目录
- data/task_templates/ — 任务级模板 YAML 文件
- data/annotations/ — 标注 JSON 文件

### Frontend (10 created, 5 modified)
- src/views/TemplateLibraryView.vue — CREATE: 模板库页
- src/components/TemplateForm.vue — CREATE: 模板创建/编辑表单
- src/components/VariableInput.vue — CREATE: 变量替换输入
- src/components/SummaryPanel.vue — CREATE: 任务摘要 Markdown 渲染
- src/components/AnnotationPanel.vue — CREATE: 标注备注组件
- src/components/ReplayView.vue — CREATE: 执行回放视图
- src/views/JobDetailView.vue — UPDATE: 添加摘要Tab + 标注面板 + 回放入口
- src/views/TasksView.vue — UPDATE: 模板选择器对接变量替换
- src/views/HistoryView.vue — UPDATE: 支持按标签筛选
- src/router/index.ts — UPDATE: 添加 /templates 路由
- src/api/index.ts — UPDATE: 添加模板+标注 API
- src/types/index.ts — UPDATE: 添加模板+标注类型
- src/layouts/MainLayout.vue — UPDATE: 侧边栏添加模板库入口

## Step-by-Step Tasks

### Task 1: 后端 — 任务模板 API
- GET/POST/PUT/DELETE /api/task-templates: YAML 文件 CRUD
- 变量替换: {{variable}} 语法
- 注意: 任务级模板 != 现有 DAG 模板 (templates/)
- VALIDATE: pytest

### Task 2: 后端 — 标注 API
- GET/PUT /api/jobs/{id}/annotations: data/annotations/{job_id}.json
- 标注结构: tags[], notes, rating(1-5), updated_at
- VALIDATE: pytest

### Task 3: 前端 — 模板库页
- 模板列表 (按 category 分组)
- 模板创建/编辑表单
- 变量替换预览: 选模板 -> 填变量 -> 预览 prompt -> 提交任务
- VALIDATE: npm run build

### Task 4: 前端 — 任务摘要渲染
- SummaryPanel: Markdown 渲染 (markdown-it)
- 任务详情页添加 "摘要" Tab
- 展示: title + content + 生成时间
- VALIDATE: npm run build

### Task 5: 前端 — 标注备注组件
- AnnotationPanel: 标签输入 + 备注文本框 + 1-5星评分
- History 页面按标签筛选
- VALIDATE: npm run build

### Task 6: 前端 — 执行回放视图
- ReplayView: 垂直时间轴 + 逐步播放/暂停
- 关键节点不同颜色: workflow.start, workflow.end, agent.tool_use, error
- 耗时统计
- VALIDATE: npm run build

### Task 7: 路由 + 导航更新
- /templates -> TemplateLibraryView
- MainLayout 侧边栏添加 "模板库" 入口
- VALIDATE: npm run build

## Validation
- cd weave_ui/frontend && npm run build
- python -m pytest tests/test_weave_ui_*.py -x -q

## NOT Building
- DAG 模板管理 (已有 /api/templates)
- SQLite 标注存储 (MVP 用 JSON)
