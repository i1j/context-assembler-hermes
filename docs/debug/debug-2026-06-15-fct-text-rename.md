# 调试记录 2026-06-15 — `_is_valid_fct` / `_format_fct_for_display` 残余旧名

**提交**: `403255f`
**日期**: 2026-06-15 12:12

## 问题

`_is_valid_fct` 和 `_format_fct_for_display` 中仍有残余重命名错误：函数体引用 `fct_text`，
但对应的传入参数已重命名为 `l1_text`。

## 根因

术语统一重构（Phase 1，`260000d`）将 DB 列名从 `l1_text` → `Fct`，Python 标识符从 `l1_text` → `fct_text`。
但这两处的函数参数名在重命名时被遗漏，函数体引用的 `fct_text` 变量名与参数名不匹配，
导致 `NameError` 或静默取到错误值。

## 修复

```python
# 改前：def _format_fct_for_display(Fct, ...) 体内用 fct_text
# 改后：def _format_fct_for_display(fct_text, ...) 参数名与体内一致
# 同理 _is_valid_fct
```

## 文件变更

```
ca/__init__.py | 4 +-
tests/ | 大量更新
```

## 验证

269 ✅ / 19 ⏭️ / 0 ❌（原 241✅/49❌/22⏭️）。
