# Survey Agent 前端与适配器后端服务器迁移清单

> 目标：迁移 `D:\code\survey_agent` 的前端与适配器后端，继续复用 RAG-Anything 和既有连铸电磁搅拌知识库。本文不把 `raganything-pro` 整体视为应用后端。

## 1. 架构边界

| 层级 | 本机目录 | 服务器职责 | 迁移策略 |
|---|---|---|---|
| 前端 | `survey_agent/frontend` | React 研究工作台 | 从独立 GitHub 仓库拉取并构建 |
| 应用后端 | `survey_agent/backend` | API、SSE、Memory、综述任务、证据与引用 | 迁移完整 `survey_agent` 项目 |
| RAG 适配器 | `survey_agent/backend/rag_client.py` | 隔离应用与 RAG-Anything API | 与应用后端一起迁移 |
| RAG 核心 | `raganything-pro/RAG-Anything` | LightRAG、解析与检索底层 | 固定版本或迁移当前 fork，不混入前端仓库 |
| 知识库 | `raganything-pro/data/rag_storage/...` | 只读向量/图谱存储 | 单独按数据卷迁移 |
| 应用数据 | `survey_agent/data`、`workspace` | 会话、记忆、反馈、任务检查点 | 停服后备份迁移 |

## 2. 当前版本基线

- Python：`3.10.20`
- Node.js：`24.11.0`（服务器建议 Node 20/22 LTS；只运行构建产物时不需要 Node）
- npm：`11.6.1`
- RAG-Anything 上游基线 commit：`b7ba3c8cfebd0e89c330a449b1f77624174dfbcd`
- 前端仓库：`git@github.com:xs-web-lyq/survey-agent-frontend.git`
- 前端首个发布 commit：`b7483df`
- 当前知识库：`casting_ems_v1_plus_v2_core`
- Embedding：`BAAI/bge-large-zh-v1.5`，维度 `1024`
- LLM 协议：OpenAI-compatible（阿里云百炼）

### 迁移闸门：RAG-Anything 本地修改

当前 RAG-Anything 工作树不是纯上游版本。核心包至少存在以下历史修改：

- `raganything/batch_parser.py`
- `raganything/config.py`
- `raganything/parser.py`
- `raganything/processor.py`
- 未跟踪的 `raganything/path_utils.py`

服务器部署前必须二选一：

1. **推荐**：把这些修改提交到个人 fork，服务器克隆 fork 并固定 commit；
2. 导出补丁并在服务器对 `b7ba3c8` 应用，同时单独复制未跟踪文件。

不要直接克隆上游原版后跳过此检查，否则解析路径、批处理或存储行为可能不一致。

## 3. 应用后端改动清单

### 3.1 配置与模型适配

- `backend/config.py`：集中读取路径、模型、Embedding、Memory 和服务配置。
- `backend/llm.py`：OpenAI/Anthropic 协议统一入口、流式输出和指数退避重试。
- `backend/rag_client.py`：RAG-Anything 唯一适配入口；注入源码路径、LLM 回调和本地 Embedding。

### 3.2 API、事件与数据持久化

- `backend/server.py`：FastAPI、SSE、会话、Memory、综述任务、证据矩阵、导出及前端静态托管。
- `backend/events.py`：Thinking、工具调用、检索、Memory 和任务状态事件。
- `backend/db.py`：SQLite 会话、消息、反馈和会话管理。
- `backend/conversation_export.py`：会话 Markdown 导出。
- `backend/export_finetune.py`：反馈数据导出。
- `backend/images.py`：文献图片索引与图片回链。

### 3.3 问答、头脑风暴与综述

- `backend/pipelines/qa.py`：问答路由、深度循环检索、证据反思和引用生成。
- `backend/pipelines/brainstorm.py`：多轮选题探讨和结构化 Research Brief。
- `backend/agent/phases.py`：综述阶段执行、章节写作、补证和终稿整合。
- `backend/agent/prompts.py`：综述和证据评估提示词。
- `backend/agent/state.py`：任务状态和恢复数据结构。
- `backend/task_manager.py`：任务创建、检查点恢复、终稿重试和定向补证。

### 3.4 Evidence 与文献信息

- `backend/agent/evidence_coverage.py`：按研究问题评估证据覆盖，而不是按 chunk 数量判断。
- `backend/agent/evidence_store.py`：证据矩阵版本化落盘。
- `backend/bibliography.py`：标题、作者、年份、期刊、DOI 等文献信息补全。
- `backend/tools/retrieval.py`：检索工具与文献范围约束。
- `backend/tools/verify.py`：引用和证据校验。
- `backend/tools/files.py`：工作区文件边界、原子写入和任务产物管理。

### 3.5 多轮 Memory

需迁移整个 `backend/memory/`：

- `models.py`：Memory 数据模型。
- `context.py`：近期消息、摘要和长期记忆组装。
- `rewrite.py`：追问消歧与独立检索问题改写。
- `extract.py`：长期偏好、目标和决策抽取。
- `compact.py`：长对话压缩摘要。
- `store.py`：SQLite Memory 持久化。
- `service.py`：Memory 生命周期编排。
- `__init__.py`：统一服务入口。

## 4. 前端迁移清单

前端已经是独立仓库，服务器拉取：

```bash
git clone git@github.com:xs-web-lyq/survey-agent-frontend.git frontend
```

主要改动模块：

- `src/pages/ChatPage.tsx`：流式问答、临时消息、Memory 和会话状态。
- `src/components/ConversationSidebar.tsx`：搜索、重命名、Markdown 导出、删除和弹出菜单交互。
- `src/components/ThinkingPanel.tsx`：可审计 Thinking 与工具/检索轨迹。
- `src/components/MemoryPanel.tsx`：分层 Memory 查看与管理。
- `src/pages/BrainstormPage.tsx`：选题探讨和综述任务交接。
- `src/components/EvidenceMatrix.tsx`：研究问题覆盖与定向补证。
- `src/pages/SurveyList.tsx`、`SurveyDetail.tsx`：综述任务和检查点界面。
- `src/lib/api.ts`、`sse.ts`：REST 与 SSE 客户端。
- `src/index.css`、`useTheme.ts`：主题、动效和侧边栏层级样式。

不迁移：

- `frontend/node_modules/`
- `frontend/dist/`（推荐服务器重新构建；也可将已验证构建产物作为发布包复制）
- `frontend/.playwright-cli/`

## 5. 数据迁移范围与当前体量

| 数据 | 当前体量 | 必需性 | 说明 |
|---|---:|---|---|
| RAG Storage | 约 1.37 GB | 必需 | 图谱、向量、文档状态等运行知识库 |
| Academic Index | 约 36.7 MB | 推荐 | progressive 路由和章节学术索引 |
| `survey_agent/data` | 约 9 MB | 保留历史时必需 | `feedback.db`、`image_index.json`、Crossref 缓存 |
| `survey_agent/workspace` | 约 3.4 MB | 保留综述任务时必需 | 任务检查点、证据矩阵、终稿产物 |
| 前端 `dist` | 约 1.9 MB | 构建后必需 | FastAPI 静态托管内容 |
| MinerU parsed | 约 13.1 GB | 可选 | 原文图片回链；不复制则应关闭或重建图片索引 |
| RAG-Anything `output` | 约 71 MB | 可选 | 额外解析产物和图片来源 |

本机对应路径：

```text
D:\code\raganything-pro\data\rag_storage\casting_ems_v1_plus_v2_core
D:\code\raganything-pro\data\indexes\casting_ems_v1_plus_v2_core_academic_index
D:\code\raganything-pro\data\parsed
D:\code\raganything-pro\RAG-Anything\output
D:\code\survey_agent\data
D:\code\survey_agent\workspace
```

### SQLite 迁移要求

`data/feedback.db` 保存会话、消息、反馈和 Memory。迁移时：

1. 先停止本机后端；
2. 确认没有正在写入的 `feedback.db-wal`；
3. 备份并复制 `feedback.db`；
4. 在服务器启动前校验文件大小和校验和；
5. 数据目录仅授予服务用户读写权限。

若不需要迁移历史会话，可以不复制 `survey_agent/data`，应用会创建新数据库；但需要重新生成 `image_index.json` 才能显示图片回链。

## 6. 推荐服务器目录

```text
/opt/survey-agent/                 # 应用源码
/opt/survey-agent/frontend/        # 前端源码和 dist
/opt/rag-anything/                 # 固定版本的 RAG-Anything
/srv/survey-agent/app-data/        # feedback.db、image_index.json
/srv/survey-agent/workspace/       # 综述任务与检查点
/srv/survey-agent/rag-storage/     # 知识库存储
/srv/survey-agent/indexes/         # Academic Index
/srv/survey-agent/parsed/          # 可选解析产物
```

## 7. 服务器 `.env` 模板

禁止复制本机 `.env` 中的明文密钥。服务器从 `.env.example` 新建：

```env
RAG_ANYTHING_REPO=/opt/rag-anything
RAG_STORAGE_DIR=/srv/survey-agent/rag-storage/casting_ems_v1_plus_v2_core
ACADEMIC_INDEX_PATH=/srv/survey-agent/indexes/casting_ems_v1_plus_v2_core_academic_index/academic_index.json
PARSER_OUTPUT_DIRS=/srv/survey-agent/parsed;/opt/rag-anything/output

LLM_BINDING_TYPE=openai
LLM_BINDING_API_KEY=由服务器密钥管理系统注入
LLM_BINDING_HOST=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-plus
WRITER_MODEL=qwen-max
LLM_MAX_RETRIES=2
LLM_RETRY_BASE_SECONDS=0.8

EMBEDDING_MODEL_NAME=BAAI/bge-large-zh-v1.5
EMBEDDING_DIM=1024
EMBEDDING_DEVICE=cuda

WORKSPACE_DIR=/srv/survey-agent/workspace
DATA_DIR=/srv/survey-agent/app-data
SERVER_HOST=127.0.0.1
SERVER_PORT=8000

MEMORY_RECENT_MESSAGES=8
MEMORY_COMPACT_AFTER_MESSAGES=16
MEMORY_COMPACT_AFTER_CHARS=24000
MEMORY_MAX_DURABLE_ITEMS=5
MEMORY_IDLE_EXTRACT_SECONDS=600
MEMORY_LLM_REWRITE=false
```

如果服务器没有可用 CUDA，将 `EMBEDDING_DEVICE=cpu`。Embedding 模型和维度必须与入库时一致，不能随意更换。

## 8. 环境安装与前端构建

### 8.1 Python

```bash
cd /opt/survey-agent
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# GPU 服务器先安装与 CUDA 匹配的 PyTorch
pip install -e /opt/rag-anything
pip install -r requirements.txt
```

`RAG-Anything` 要求 Python `>=3.10`。建议锁定依赖版本并保存服务器实际安装的 `pip freeze`。

### 8.2 前端

```bash
cd /opt/survey-agent/frontend
npm ci
npm run build
```

当前前端全部使用同源 `/api`，最简单的生产方式是让 FastAPI 直接托管 `frontend/dist`。如果前后端分域部署，需要增加 API Base URL 配置并收紧 CORS。

## 9. 生产启动方式

当前任务管理器和 EventBus 依赖单进程内存状态，本阶段必须使用 **1 个 Uvicorn worker**：

```bash
cd /opt/survey-agent
source .venv/bin/activate
python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000 --workers 1
```

Systemd 示例：

```ini
[Unit]
Description=Survey Agent
After=network.target

[Service]
Type=simple
User=survey-agent
Group=survey-agent
WorkingDirectory=/opt/survey-agent
EnvironmentFile=/opt/survey-agent/.env
ExecStart=/opt/survey-agent/.venv/bin/python -m uvicorn backend.server:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
RestartSec=5
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

Nginx/反向代理必须针对 SSE 设置：

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}
```

## 10. 上线验收检查点

### Checkpoint A：路径与依赖

```bash
cd /opt/survey-agent
source .venv/bin/activate
python -c "from backend.config import settings; print(settings.validate_paths())"
python -c "import torch; print(torch.cuda.is_available())"
python -c "from backend.rag_client import get_rag; import asyncio; asyncio.run(get_rag()); print('rag-ok')"
```

期望：路径错误列表为空，RAG 初始化成功。

### Checkpoint B：API 与模型

```bash
curl http://127.0.0.1:8000/api/meta
curl http://127.0.0.1:8000/api/conversations
```

再执行一次最小 LLM 补全，确认百炼 Key、地址、模型名和地域一致。

### Checkpoint C：核心业务

- 创建新会话并完成一次流式问答；
- Thinking、工具调用和引用正常显示；
- 刷新页面后会话和 Memory 仍存在；
- Markdown 导出、重命名和删除正常；
- 头脑风暴能够生成 Research Brief 并创建综述任务；
- 综述任务能够暂停、恢复、补证和导出；
- 引用图片能够从新服务器路径打开。

### Checkpoint D：重启恢复

- 在任务执行中重启服务；
- 检查任务状态、检查点和 `events.jsonl`；
- 验证已完成章节不会重复生成；
- 验证 SQLite 数据无损。

## 11. 不应上传或迁移到 GitHub 的内容

- `.env` 和任何 API Key；
- `data/feedback.db` 及用户会话；
- `workspace/` 中的私有综述任务；
- `rag_storage/`、向量文件和原始论文；
- MinerU 解析产物；
- `node_modules/`、`dist/`、`__pycache__/`；
- `*.log`、`*.err`、Playwright 临时文件；
- Hugging Face、DashScope 或其他服务的缓存凭据。

## 12. 推荐发布与回滚方式

每个阶段使用独立提交和标签：

```text
checkpoint/migration-source
checkpoint/migration-data
checkpoint/migration-runtime
v0.1.0-server
```

服务器采用 release 目录和软链接：

```text
/opt/survey-agent/releases/<commit>/
/opt/survey-agent/current -> releases/<commit>/
```

上线前备份 `feedback.db` 和 `workspace`。回滚时切换 `current` 软链接并恢复对应数据库备份，避免新旧数据库结构混用。

## 13. 迁移前仍需完成的事项

- 为完整 `survey_agent` 项目创建后端/全栈远程仓库；
- 提交或导出 RAG-Anything 本地核心修改；
- 增加 `requirements-dev.txt` 和可运行的 pytest 环境；
- 完成 P0 问答失败状态持久化；
- 增加数据库 schema migration、备份和软删除；
- 增加 `/healthz`、`/readyz` 和模型预检；
- 将生产 CORS、错误脱敏和密钥管理配置化。
