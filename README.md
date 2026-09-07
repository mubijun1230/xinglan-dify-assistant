# 南极业务助手

南极人内部 Agent：钉钉入口、意图分流 Chatflow、OA/制度与工程项目 RAG、类目日销只读 NL2SQL。

基于私有化 [Dify](https://github.com/langgenius/dify) 官方镜像；本仓库是业务编排、自研网关/钉钉桥与运行时补丁。

| 意图 | 路径 |
| --- | --- |
| OA / 制度 | 知识库；未命中拒答 |
| 工程项目 | 知识库（工程名称/负责人/状态）；未命中拒答 |
| 经营取数 | 模型只出 SELECT，`query-gateway` 校验执行；数字不再二次改写 |
| 职责外 | 固定文案 |

```text
钉钉 Stream → dingtalk-bridge → Dify Chatflow
                    ├─ RAG（OA/制度、工程项目）
                    └─ NL2SQL → query-gateway → nanjiren_cate_date
```

```text
dingtalk-bridge/   钉钉 ↔ Dify
query-gateway/     只读取数网关
knowledge-bases/   OA、工程项目、制度
docker/custom/     镜像补丁与工作流脚本
docs/              工作流说明
```

```bash
copy docker\.env.example docker\.env
copy query-gateway\.env.example query-gateway\.env
copy dingtalk-bridge\.env.example dingtalk-bridge\.env
cd docker && docker compose up -d
```

打开 http://localhost 初始化，导入知识库后执行 `python /tmp/patch_workflow.py`（按本机 App/Dataset UUID 修改脚本）。

节点 ID 仅 `[a-zA-Z0-9_]`；HTTP `retry_interval` 须为整数。

补丁说明：[docker/custom/README.md](docker/custom/README.md)

MIT（本仓库原创）。Dify Compose 文件为 Apache 2.0。勿提交 `.env` 与 `docker/volumes`。
