# 星澜业务助手

私有化 [Dify](https://github.com/langgenius/dify) 上的企业内部 Agent：**钉钉入口 + 意图分流 Chatflow + 知识库 RAG + 只读 NL2SQL 网关**。

本仓库是业务落地与运行时补丁，不是 Dify 官方源码镜像。Dify 以官方 Docker 镜像运行；本仓库提供编排规范、自研服务和对 graphon / API 错误管道的补丁。

## 能做什么

| 意图 | 路径 | 约束 |
| --- | --- | --- |
| OA / 制度 | 知识库检索 → LLM | 未命中则标准拒答，不编条款 |
| 客户档案 | 知识库检索 → LLM | 未命中请补录，不编接口人 |
| 经营取数 | LLM 只生成 SELECT → `query-gateway` | 模型不持有库密码；仅表白名单 |
| 闲聊 / 职责外 | 短回复或固定文案 | 不编造数字 |

取数成功后 **不再用 LLM 改写数字**。Excel / 直方图由网关生成，钉钉以文件发出。

## 架构

```text
员工 ──钉钉 Stream──► dingtalk-bridge
                         │  POST /v1/chat-messages
                         ▼
                   Dify Chatflow（业务助手）
              ┌──────────┼──────────┐
              ▼          ▼          ▼
           RAG 检索   生成 SELECT   闲聊/职责外
              │          │
              │          ▼
              │    query-gateway
              │    （校验 + 只读执行 + 导出/出图）
              ▼
           文本结论；xlsx/png 走钉钉文件通道
```

安全要点：只允许 `SELECT`/`WITH`、禁止 `UNION`/`JOIN`/多语句、表白名单、强制 `LIMIT`、会话 `READ ONLY`。

## 仓库结构

```text
dingtalk-bridge/     钉钉 Stream 机器人 ↔ Dify
query-gateway/       NL2SQL 只读网关（Flask + PyMySQL）
knowledge-bases/     OA / 客户 / 制度语料（Markdown）
docker/custom/       挂进官方镜像的源码补丁与工作流脚本
docker/              官方 Dify Compose + override（不含 volumes）
docs/                工作流规范
```

## 快速开始

1. 安装 Docker Desktop，复制环境变量模板（**不要提交 `.env`**）：

```bash
copy docker\.env.example docker\.env
copy query-gateway\.env.example query-gateway\.env
copy dingtalk-bridge\.env.example dingtalk-bridge\.env
```

2. 在 `docker/.env` 填 Dify `SECRET_KEY` 等；在模型供应商中配置 LLM。  
3. 在 `query-gateway/.env` 填只读库账号，并把 `QUERY_API_TOKEN` 与 Chatflow HTTP 节点一致。  
4. 启动（Windows 也可用根目录 `启动Dify.bat`）：

```bash
cd docker
docker compose up -d
```

5. 打开 http://localhost 完成初始化，创建 Chatflow「业务助手」，导入 `knowledge-bases/` 三个知识库。  
6. 在 API 容器中执行 `python /tmp/patch_workflow.py`（需先按本机 Dataset / App ID 修改脚本中的 UUID），或按 `docs/业务助手-工作流规范.md` 在画布搭出同等分流。  
7. 配置钉钉开放平台 Stream 应用后启动 `dingtalk-bridge`。

工作流节点 ID 只能使用 `[a-zA-Z0-9_]`，否则 Answer 模板不会替换。HTTP 节点 `retry_interval` 必须是整数。

## 源码补丁（相对「只调 API」）

见 [docker/custom/README.md](docker/custom/README.md)。补丁用 volume 覆盖官方镜像内文件，便于对照 diff：错误中文化、graphon 重试间隔类型、Chatflow 可复现导出。

## 演示问句

- 请假超过 3 天要谁审批？
- 宁泊汽车的客户成功经理是谁？
- 按渠道汇总今年支付金额并导出 Excel
- 按渠道汇总今年 1–7 月支付金额并用直方图展示

## 许可

原创代码：MIT（见 `LICENSE`）。`docker/` 中来自 Dify 官方部署的文件：Apache 2.0（见 `NOTICE`）。

请勿把真实密钥、`docker/volumes` 或生产库数据推送到 GitHub。
