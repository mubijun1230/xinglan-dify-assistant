# Dify 补丁

`docker-compose.override.yml` 挂载进官方镜像。

| 文件 | 作用 |
| --- | --- |
| `error_i18n.py` | 模型错误转中文 |
| `patches/based_generate_task_pipeline.py` | 对话错误走同一套中文 |
| `patches/base_app_generate_response_converter.py` | 生成响应错误中文化 |
| `patches/base_node_data.py` | `retry_interval` float→int |
| `patch_workflow.py` | 南极业务助手 Chatflow |
| `import_kb.py` | 导入知识库 |

`QUERY_API_TOKEN` 与网关 `.env` 保持一致。
