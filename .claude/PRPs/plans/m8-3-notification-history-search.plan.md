# Plan: M8.3 — 通知系统 + 历史归档与搜索

## Summary
实现两个并行能力：(1) 任务状态变更时推送浏览器通知，(2) 已完成任务的历史归档和搜索。

## Metadata
- Complexity: Large
- Source Issue: #1100 (Parent: #1097)
- Depends on: M8.2
- Estimated Files: 14

## Files to Change

### Backend (1 modified)
- weave_ui/server.py — UPDATE: 添加 GET/PUT /api/notification-preferences, GET /api/search

### Frontend (9 created, 4 modified)
- src/composables/useNotification.ts — CREATE: 浏览器通知服务
- src/stores/notification.ts — CREATE: 通知偏好状态
- src/components/NotificationSettings.vue — CREATE: 通知偏好设置
- src/components/SearchBar.vue — CREATE: 搜索栏组件
- src/components/JobSearchResult.vue — CREATE: 搜索结果卡片
- src/views/HistoryView.vue — UPDATE: 重写为搜索+筛选历史页
- src/views/SettingsView.vue — UPDATE: 添加通知偏好子页
- src/components/JobCard.vue — UPDATE: 添加通知高亮效果
- src/stores/job.ts — UPDATE: 添加 searchJobs action
- src/api/index.ts — UPDATE: 添加 searchJobs, getNotificationPrefs, updateNotificationPrefs
- src/types/index.ts — UPDATE: 添加 SearchParams, NotificationPrefs 类型

## Step-by-Step Tasks

### Task 1: 后端 — 通知偏好 API
- GET/PUT /api/notification-preferences: JSON 文件持久化 (data/notification_preferences.json)
- 配置矩阵: 任务完成/失败/卡死/审批 x 开/关
- MIRROR: server.py 现有 API 模式
- VALIDATE: pytest

### Task 2: 后端 — 全文搜索 API
- GET /api/search?q=&status=&from=&to=
- 搜索范围: Job requirement, Job ID, Session 事件摘要, 标注备注
- 实现: 内存过滤 (遍历 JobRepository + SessionStore)
- 返回: 匹配 Job 列表 + 高亮片段
- VALIDATE: pytest

### Task 3: 前端 — 通知 Composable
- useNotification.ts: 请求 Notification 权限, 4 种触发类型
- WebSocket 事件 -> 检测状态变更 -> 匹配通知规则 -> Notification()
- STUCK 检测: 15min 无事件
- VALIDATE: TypeScript 编译

### Task 4: 前端 — 通知偏好设置页
- SettingsView.vue: 添加 NSwitch 矩阵 (完成/失败/卡死/审批 x 开/关)
- 调用 GET/PUT /api/notification-preferences
- VALIDATE: npm run build

### Task 5: 前端 — 历史搜索页 (HistoryView.vue)
- 搜索框: 输入即搜 (debounce 300ms)
- 筛选器: 状态 NSelect + 时间 NDatePicker
- 列表: Job ID + requirement 摘要 + 状态 + 完成时间 + 搜索高亮
- 分组: 今日 / 本周 / 更早
- VALIDATE: npm run build

### Task 6: JobCard 通知高亮
- 收到通知时卡片闪烁/高亮动画
- VALIDATE: npm run build

## Validation
- cd weave_ui/frontend && npm run build
- python -m pytest tests/test_weave_ui_*.py -x -q

## NOT Building
- 邮件/Webhook 通知渠道 (仅浏览器通知)
- SQLite 搜索引擎 (MVP 用内存过滤)
