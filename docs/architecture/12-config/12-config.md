---
title: 配置体系
slug: config
category: architecture
version_introduced: v5.0
status: 已实装
decisions: [config-system]
depends_on: []
updated: 2026-07-26
source_files: ["ca/config.py", "ca/settings.yaml"]
---

## 问题

CA 插件多个模块需要配置参数（嵌入后端、摘要模型、尾巴保护区大小等）。需要统一、可覆盖的配置体系。

## 双层配置

```yaml
优先级: 环境变量 (CA_*)  >  settings.yaml  >  内建默认值
```

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| 尾巴保护区 token 数 | `CA_PROTECT_TAIL_TOKENS` | 10000 | 保护最后 N user 轮（无 settings.yaml 时兜底；settings.yaml 默认 20000） |
| 最大对话轮数 | `CA_MAX_TURNS` | 1000 | 超过则丢弃最早 |
| 嵌入后端 | `CA_EMBED_BACKEND` | `ollama` | `ollama` / `sentence-transformers` / `fallback` |
| 摘要模型 | `CA_LLM_MODEL` | `qwen3-4b-instruct:latest` | LLM 摘要用模型 |
| 嵌入模型 | `CA_EMBED_MODEL` | `dengcao/Qwen3-Embedding-0.6B:Q8_0` | 向量嵌入用 |
| 嵌入端点 | `CA_EMBED_ENDPOINT` | `http://127.0.0.1:11435` | Ollama 嵌入服务地址 |
| 嵌入超时 | `CA_EMBED_TIMEOUT` | 10 | 单次嵌入请求超时（秒） |
| 嵌入重试 | `CA_EMBED_MAX_RETRIES` | 2 | 嵌入失败重试次数 |
| 批量并行超时 | `CA_EMBED_BATCH_PARALLEL_TIMEOUT` | 15 | 批量并行嵌入单条超时（秒） |

## 关键约束

- settings.yaml 中的配置项可被同名的 `CA_` 前缀环境变量覆盖
- 启动时加载一次，运行中不热重载
- 非法值回退到内建默认值（日志警告）
