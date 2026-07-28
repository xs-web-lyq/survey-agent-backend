# P1-C 综述终稿与质量门禁

## 设计目标

综述任务“生成完成”不等于“满足论文提交要求”。P1-C 在生成链路与交付链路之间增加确定性质量门禁，使 Research Brief、证据矩阵、正文引用和参考文献元数据形成可追踪闭环。

## 数据流

```text
Research Brief + Evidence Matrix + Section Drafts
  -> 终稿整合约束
  -> survey.md
  -> 引用原文核查
  -> bibliography.json / references.md
  -> quality_report.json
  -> 前端质量报告 / ZIP 完整导出
```

## 终稿整合约束

终稿模型收到的是“结构化约束 + 章节草稿”，并遵守以下规则：

1. 引言沿用 Research Brief 的研究范围与纳入、排除边界。
2. 结论逐项回应简报研究问题。
3. 证据矩阵中未覆盖的问题只能表述为研究空白，不能改写成肯定结论。
4. 章节已有引用标记保持不变，终稿阶段不新增无证据论断。

整合约束只包含问题、覆盖状态和停止原因，不包含完整 chunk 正文，避免重复扩大模型上下文。

## 确定性门禁

`quality_report.json` 不调用大模型自评，完全由持久化事实计算：

- 研究简报问题是否分配、是否被证据覆盖。
- 所有章节研究问题是否满足证据块与独立来源门槛。
- 正文唯一引用是否通过原文核查。
- 参考文献记录是否具备对应类型要求的作者、年份、来源、卷期页码等字段。

任一关键门禁未通过时，状态为 `review_required`；全部通过时为 `ready`。报告同时给出可执行建议、失败 chunk ID 和待补全文献字段。

## 产物边界

- `survey.md`：可读正文，失败引用保留 `⚠`，不静默删除。
- `references.md`：格式化参考文献及元数据完整度。
- `bibliography.json`：完整结构化文献记录。
- `evidence_matrix.json`：研究问题覆盖事实。
- `quality_report.json`：交付门禁及人工处理清单。
- `export.zip`：包含上述全部产物及正文图片。

## 代码位置

- `backend/agent/survey_quality.py`：终稿约束和质量门禁计算。
- `backend/agent/phases.py`：终稿、引用核查和报告落盘。
- `backend/server.py`：质量报告 API 与完整导出。
- `frontend/src/components/SurveyQualityReport.tsx`：交付门禁 UI。
