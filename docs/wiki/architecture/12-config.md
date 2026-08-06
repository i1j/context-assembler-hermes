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
| 尾巴保护区 token 数 | `CA_PROTECT_TAIL_TOKENS` | 20000 | 保护最后 N user 轮 |
| 最大对话轮数 | `CA_MAX_TURNS` | 1000 | 超过则丢弃最早 |
| 嵌入后端 | `CA_EMBEDDING_BACKEND` | `ollama` | `ollama` / `sentence-transformers` |
| 摘要模型 | `CA_SUMMARY_MODEL` | `qwen3:4b` | LLM 摘要用模型 |
| 嵌入模型 | `CA_EMBEDDING_MODEL` | `qwen3-embedding-0.6B` | 向量嵌入用 |

## 关键约束

- settings.yaml 中的配置项可被同名的 `CA_` 前缀环境变量覆盖
- 启动时加载一次，运行中不热重载
- 非法值回退到内建默认值（日志警告）
