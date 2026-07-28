# P1 Research Brief 架构

## 目标

Research Brief 是“选题头脑风暴”和“综述写作代理”之间的显式领域契约。它保存研究问题、
纳入/排除边界、证据缺口和文献范围，避免交接信息被压平成不可审计的 prompt 文本。

## 生命周期

```text
brainstorm conversation
        |
        v
draft --编辑--> draft --确认--> confirmed --交接--> handed_off
  \                         \                         |
   \--再次 conclude 生成 vN+1 \--编辑后回到 draft    +--> survey task
```

约束：

- 同一头脑风暴会话可以生成多个版本，`(conv_id, version)` 唯一；
- `readiness_score`、证据数量、检索轮次和文献范围由系统生成，普通编辑接口不能修改；
- 确认前至少需要一个有效主题、两个研究问题和一条纳入边界；
- `handed_off` 后 Brief 不可修改；
- Brief 交接使用确定性 `task_id`，重复请求返回同一任务；
- 综述 `task.json` 同时保存 `research_brief_id` 和完整结构化 Brief。

## API

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/brainstorm/{conv_id}/conclude` | 生成并持久化下一版本草稿 |
| GET | `/api/brainstorm/{conv_id}/briefs` | 查询版本历史 |
| GET | `/api/brainstorm/{conv_id}/briefs/latest` | 恢复最新版本 |
| GET | `/api/research-briefs/{brief_id}` | 查询单个 Brief |
| PATCH | `/api/research-briefs/{brief_id}` | 编辑草稿；已确认版本编辑后回到草稿 |
| POST | `/api/research-briefs/{brief_id}/confirm` | 校验并确认 |
| POST | `/api/research-briefs/{brief_id}/handoff` | 创建综述任务并记录交接 |

## 数据边界

- SQLite `research_briefs`：版本、状态、结构化 Brief、检索 scope、任务关联；
- Conversation：保留选题讨论原始记录；
- Survey workspace：保存交接后的任务快照和生成产物；
- RAG-Anything：仍作为外部检索依赖，不承担应用领域状态。

## 后续 P1-B 接口

P1-B 将直接读取 Brief 的 `research_questions`、`inclusion_criteria`、
`exclusion_criteria` 和 `evidence_gaps`，为每个研究问题建立覆盖矩阵、扩展查询与停止条件。
