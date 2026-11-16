# 工程招投标 AI · 第一阶段 MVP

使用企业内网 Dify 和本地模型，从项目资料生成可复核的技术标草稿。包含 WF01–WF06、WF09、WF10、PostgreSQL 初始化脚本、工作台、文件解析、知识资料版本管理和 Word/Excel 导出。

## 快速开始

环境需要 Python 3.12、运行中的 Docker Compose、私有 Dify 和本地 OpenAI 兼容模型接口。后端与 Dify 独立启动，已有 Dify 可以继续使用。

```powershell
python scripts/init_environment.py
docker compose config --quiet
docker compose up -d --build
```

1. 从生成的 `.env` 取编制和复核令牌。将 `BIAOSHU_MODEL_BASE_URL` 和 `BIAOSHU_MODEL_NAME` 配成内网模型，再重启 API。浏览器打开 `http://127.0.0.1:8091` 可使用工作台，接口契约见 `/openapi.json`。
2. 在 Dify 工作室导入 [workflows](workflows) 中八个 DSL 文件。分别填写 `BIAOSHU_API_BASE` 和 `BIAOSHU_API_TOKEN`。API 地址需要从 Dify 执行 HTTP 节点的环境访问。详细连网步骤见[部署与运维](doc/10_部署与运维规范.md)。
3. 用 WF01 创建项目、上传文件并归档。复核账号确认档案后，执行 WF02，复核原文、补遗和冲突，再执行 WF03、WF04、WF05。
4. 确认证据候选后按技术标目录执行 WF06。编制人可修订章节，复核人确认章节版本。执行 WF09 审查，WF10 导出 Word 草稿。审核稿复用已确认草稿的字节，并要求当前章节、审查报告和草稿都已通过复核。

仅预览后端时可执行 `python -m pip install -r requirements-test.txt`、`python scripts/init_environment.py` 和 `python scripts/run_local.py`，直接启动回环工作台与本地 SQLite。正式部署使用 PostgreSQL。

## 文件和验证入口

| 入口 | 内容 |
| --- | --- |
| [项目总体规划](doc/00_项目总体规划.md) | 第一阶段范围、完成边界和验收 |
| [Dify 工作流设计](doc/02_Dify工作流设计.md) | 八份 DSL 导入、独立运行和原生节点校验 |
| [企业知识库](doc/03_企业知识库设计.md) | 八类资料、来源、版本、生效日、审批和检索 |
| [数据库](doc/04_数据库设计.md) | ER 图、初始化及重复执行方式 |
| [部署与运维](doc/10_部署与运维规范.md) | Docker、模型、对象存储、文件转换与备份 |
| [接口契约](doc/11_接口文档.md) | 工作流、项目、审核、资料库和成果下载 |
| [实施进度与验证](doc/12_项目实施进度.md) | 仓库基线、验证口径、缺口与交接 |

第一版 Chatflow 与使用说明保存在根目录以供对照。企业资料试跑使用 `workflows/` 下的第一阶段工作流和内网模型。商务资信编制、报价与 ComfyUI 的接入留给后续阶段。当前八份 DSL 已用 Dify 1.7.2 官方节点模型校验，也已验证各路由的 HTTP 契约。真实 Dify 实例导入和真实工程试跑还需要部署现场验证，详见实施进度。
