# Dify 运行时补丁

这些文件通过 `docker-compose.override.yml` 挂进官方镜像，**不修改镜像层**。升级 Dify 版本后需对照源码重合。

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `error_i18n.py` | 模型/插件英文错误转成可执行的中文说明 | 钉钉员工读不懂原始 JSON |
| `patches/based_generate_task_pipeline.py` | 对话错误管道调用 `translate_error` | 控制台与钉钉同一套文案 |
| `patches/base_app_generate_response_converter.py` | 生成响应里的错误同样中文化 | 流式/阻塞回复一致 |
| `patches/base_node_data.py` | `RetryConfig.retry_interval` 接受 float 并收成 int | 画布默认 0.5，graphon/Pydantic 只要整数 |
| `patch_workflow.py` | 把「业务助手」Chatflow 写成可复现图 | 意图分流、空检索拒答、取数失败兜底 |
| `import_kb.py` | 按目录导入三个知识库 | 可重复部署语料 |

`patch_workflow.py` 里 HTTP 节点的 Bearer 从环境变量 `QUERY_API_TOKEN` 读取，缺省为占位符 `CHANGE_ME_QUERY_TOKEN`，发布前请改成与 `query-gateway/.env` 相同的值。
