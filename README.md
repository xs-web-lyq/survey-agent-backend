# Survey Agent Backend

面向材料冶金文献研究的 Survey Agent 应用后端。该仓库负责 FastAPI/SSE、多轮 Memory、RAG-Anything 适配、证据覆盖、文献元数据、选题头脑风暴和论文综述任务。

前端仓库：<https://github.com/xs-web-lyq/survey-agent-frontend>

## 架构边界

- 本仓库只包含应用后端、测试、脚本和架构文档。
- RAG-Anything 作为外部依赖，通过 `RAG_ANYTHING_REPO` 指定源码目录。
- 知识库、论文、会话数据库、任务工作区和 API Key 不进入 Git。
- 前端可由 Nginx 单独托管，也可通过 `FRONTEND_DIST_DIR` 交给 FastAPI 静态托管。

## 环境要求

- Python 3.10+
- RAG-Anything 固定版本或个人 fork
- 与知识库入库阶段一致的 Embedding 模型和维度
- OpenAI-compatible 或 Anthropic-compatible LLM 服务

## 安装

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

pip install -e /opt/rag-anything
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，至少配置：

```env
RAG_ANYTHING_REPO=/opt/rag-anything
RAG_STORAGE_DIR=/srv/survey-agent/rag-storage/casting_ems_v1_plus_v2_core
ACADEMIC_INDEX_PATH=/srv/survey-agent/indexes/casting_ems_v1_plus_v2_core_academic_index/academic_index.json

LLM_BINDING_TYPE=openai
LLM_BINDING_API_KEY=由密钥管理系统注入
LLM_BINDING_HOST=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.7-plus
WRITER_MODEL=qwen-max

EMBEDDING_MODEL_NAME=BAAI/bge-large-zh-v1.5
EMBEDDING_DIM=1024
EMBEDDING_DEVICE=cuda

DATA_DIR=/srv/survey-agent/app-data
WORKSPACE_DIR=/srv/survey-agent/workspace
FRONTEND_DIST_DIR=/opt/survey-agent/frontend/dist
```

## 启动

当前 EventBus 和任务管理器依赖单进程内存状态，生产部署请使用一个 worker：

```bash
python -m uvicorn backend.server:app \
  --host 127.0.0.1 \
  --port 8000 \
  --workers 1
```

生产环境建议在 systemd `EnvironmentFile` 或密钥管理服务中注入配置，不要把
`.env`、API Key、数据库和 workspace 上传到仓库。保持 `--workers 1`，因为当前
实时事件总线与任务管理器使用进程内状态。

健康检查分为三层：

- `GET /healthz`：仅判断进程存活，不加载 RAG、不调用模型。
- `GET /readyz`：检查数据库、必要路径和 RAG 预热状态；未就绪时返回 503。
- `POST /api/admin/preflight/model`：显式执行一次低 token 模型权限预检。生产环境
  必须配置 `ADMIN_TOKEN`，并通过 `X-Admin-Token` 请求头调用。

前后端分离部署时使用 `CORS_ORIGINS` 配置精确来源；同源部署可留空。生产环境
禁止配置通配符 `*`，否则 `/readyz` 会报告配置未就绪。

RAG 默认懒初始化，以保证健康端点和管理接口在进程启动后立即响应。若部署环境
有明确维护窗口，可设置 `STARTUP_RAG_WARMUP=true`；预热期间 `/readyz` 返回 503，
失败状态不会被伪装成 ready。

开发检查：

```bash
python scripts/check_setup.py
python -m unittest discover -s tests -p "test_*.py"
```

## 主要 API

- `POST /api/chat`：流式研究问答
- `POST /api/brainstorm`：选题头脑风暴
- `GET /api/conversations`：会话管理
- `GET /api/conversations/{id}/memory`：会话 Memory
- `POST /api/tasks`：创建综述任务
- `GET /api/tasks/{id}/events`：任务事件与检查点回放
- `GET /api/tasks/{id}/evidence-matrix`：研究问题证据覆盖矩阵
- `POST /api/tasks/{id}/evidence/supplement`：定向补证

## 数据与安全

以下内容已被 `.gitignore` 排除，禁止提交：

- `.env` 和 API Key
- `data/feedback.db`、会话与长期记忆
- `workspace/` 综述任务和产物
- RAG Storage、原始论文和解析产物
- 日志、缓存、模型权重和前端构建产物

完整服务器迁移步骤见 [docs/server_migration_checklist.md](docs/server_migration_checklist.md)。
