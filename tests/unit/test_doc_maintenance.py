"""精炼轮 Step 9 文档维护（决策 43 §4.1 链接对齐）——测试套件。

覆盖（对应需求规格 docs/requirement-step9-doc-maintenance-20260808.md §4.1）：
  T1  平铺链接断链检测（architecture/01-overview.md → 嵌套目录）
  T2  `.md/` 后缀目录链接判有效（decisions/37-reality-restructure.md 是目录）
  T3  锚点链接按 doc.md 解析忽略锚点
  T4  外部链接 http/https/mailto 跳过
  T5  断链自动修复后 os.path.exists 通过
  T6  重复执行幂等（第二次 0 断链 0 修改）
  T7  INDEX 缺行补全（新增决策 44 后 INDEX 出现 44 行，说明列为空）
  T8  trace 基准归一化（plugins/ca_assembler/__init__.py → __init__.py 存在）
  T9  trace 断链修复后全部 resolve
  T10 changelog git draft（mock git log → draft 行 + 非规范 manual）
  T11 changelog 结构校验（列数/日期格式/版本不同步）
  T12 viking:// URI 校验（OVFS 存在/缺失/不可达 graceful）
  T13 wiki/ 残留报告不产生删除
  T14 零 LLM（llm_calls == 0）
  T15 双目标（本地 + OVFS 独立扫描/落盘）
  T16 OV 侧 INDEX 修复 + 缺行仅报告
  T17 代码注释链接校验（ca/ + scripts/）；_source_to_ov_uri 未被触碰
  T18 双重错误链接（层级+平铺）basename 全局查找修正
  T19 basename 多候选 → needs_human 不自动修
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import ca.doc_maintenance as dm  # noqa: E402


# ───────────────────────── 测试构造工具 ─────────────────────────

def _make_docs(tmp_path: Path, use_md_dir_suffix: bool = False) -> Path:
    """构造迷你 docs 树（模拟本地 docs/ 结构）。

    docs/
      INDEX.md                  （平铺链接 → 断链样本）
      architecture/
        01-overview/
          01-overview.md
        ca-single.md            （平铺文件，供 basename 测试）
      decisions/
        34-idle-refinement/
          34-idle-refinement.md
        [37-reality-restructure.md/37-reality-restructure.md]  # 若 use_md_dir_suffix
      testing/
        INDEX.md
        debug-20260612-verification-report.md
        test-system-refactoring-v5.0.md
    """
    docs = tmp_path / "docs"
    (docs / "architecture" / "01-overview").mkdir(parents=True)
    (docs / "architecture" / "01-overview" / "01-overview.md").write_text("# 01-overview\n", encoding="utf-8")
    (docs / "architecture" / "ca-single.md").write_text("# single\n", encoding="utf-8")
    (docs / "decisions" / "34-idle-refinement").mkdir(parents=True)
    (docs / "decisions" / "34-idle-refinement" / "34-idle-refinement.md").write_text("# 34\n", encoding="utf-8")
    if use_md_dir_suffix:
        d = docs / "decisions" / "37-reality-restructure.md"
        d.mkdir(parents=True)
        (d / "37-reality-restructure.md").write_text("# 37\n", encoding="utf-8")
    (docs / "testing").mkdir(parents=True)
    (docs / "testing" / "debug-20260612-verification-report.md").write_text("# debug\n", encoding="utf-8")
    (docs / "testing" / "test-system-refactoring-v5.0.md").write_text("# refactor\n", encoding="utf-8")
    return docs


def _write_index(docs: Path, lines: list[str]) -> None:
    """写 INDEX.md（含表格头 + 指定数据行）。"""
    header = [
        "# CA 技术方案 Wiki\n",
        "## 架构组件（architecture/）\n",
        "| # | 文件 | 版本引入 | 源文件 | 说明 |",
        "|---|------|----------|--------|------|",
    ]
    (docs / "INDEX.md").write_text("\n".join(header + lines + [""]), encoding="utf-8")


def _make_ovfs(tmp_path: Path) -> Path:
    """构造模拟 OVFS 根（projects/context-assembler 结构）。"""
    ov = tmp_path / "ovfs" / "projects" / "context-assembler"
    (ov / "architecture" / "01-overview").mkdir(parents=True)
    (ov / "architecture" / "01-overview" / "01-overview.md").write_text("# 01-overview\n", encoding="utf-8")
    (ov / "decisions" / "34-idle-refinement").mkdir(parents=True)
    (ov / "decisions" / "34-idle-refinement" / "34-idle-refinement.md").write_text("# 34\n", encoding="utf-8")
    (ov / "testing").mkdir(parents=True)
    (ov / "INDEX.md").write_text("| # | 文件 |\n|---|------|\n| 01 | [概览](architecture/01-overview.md) |\n", encoding="utf-8")
    (ov / "test-env.md").mkdir()
    (ov / "test-env.md" / "ca-test-env.md").write_text("# test-env\n", encoding="utf-8")
    return ov


def _mk(tmp_path, **kw) -> dm.DocMaintenance:
    docs = _make_docs(tmp_path, **kw)
    ov = _make_ovfs(tmp_path)
    return dm.DocMaintenance(
        local_root=str(docs),
        ovfs_root=str(tmp_path / "ovfs"),
        ov_project="projects/context-assembler",
    ), docs, ov


# ───────────────────────── T1-T6：链接对齐 ─────────────────────────

class TestLinkScan:
    def test_t1_flat_link_detected(self, tmp_path):
        """平铺链接 architecture/01-overview.md → 断链 + suggested 嵌套路径。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, ["| 01 | [概览](architecture/01-overview.md) | v6.0 | — | 说明 |"])
        broken = m.scan_links(m.local)
        assert len(broken) == 1
        b = broken[0]
        assert b.category == "relative"
        assert b.suggested == "architecture/01-overview/01-overview.md"

    def test_t2_md_dir_suffix_valid(self, tmp_path):
        """`.md/` 后缀目录链接（decisions/37-reality-restructure.md 是目录）→ 不误报。"""
        m, docs, _ = _mk(tmp_path, use_md_dir_suffix=True)
        _write_index(docs, ["| 37 | [Reality](decisions/37-reality-restructure.md) | v7 | 检索 | reality |"])
        broken = m.scan_links(m.local)
        assert broken == []

    def test_t3_anchor_stripped(self, tmp_path):
        """锚点链接 doc.md#section 按 doc.md 解析。"""
        m, docs, _ = _mk(tmp_path)
        (docs / "guide.md").write_text("# g\n", encoding="utf-8")
        _write_index(docs, ["| 01 | [概览](architecture/01-overview/01-overview.md#toc-标题) | v6.0 | — | x |"])
        broken = m.scan_links(m.local)
        assert broken == []

    def test_t4_external_skipped(self, tmp_path):
        """外部链接 http/https/mailto/viking:// 跳过。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, [
            "| 01 | [外链](https://example.com) | v6.0 | — | x |",
            "| 02 | [OV](viking://resources/projects/context-assembler/INDEX.md) | v6.0 | — | x |",
            "| 03 | [锚点](#heading) | v6.0 | — | x |",
        ])
        broken = m.scan_links(m.local)
        assert broken == []

    def test_t5_fix_applied(self, tmp_path):
        """断链自动修复后 os.path.exists 通过，文件内容更新。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, ["| 01 | [概览](architecture/01-overview.md) | v6.0 | — | 说明 |"])
        broken = m.scan_links(m.local)
        n = m.fix_links(m.local, broken)
        assert n >= 1
        content = (docs / "INDEX.md").read_text(encoding="utf-8")
        assert "architecture/01-overview/01-overview.md" in content
        assert "architecture/01-overview.md)" not in content

    def test_t6_idempotent(self, tmp_path):
        """连续两次执行，第二次 0 断链 0 修改。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, ["| 01 | [概览](architecture/01-overview.md) | v6.0 | — | 说明 |"])
        broken1 = m.scan_links(m.local)
        m.fix_links(m.local, broken1)
        broken2 = m.scan_links(m.local)
        assert broken2 == []


# ───────────────────────── T7：INDEX 维护 ─────────────────────────

class TestIndex:
    def test_t7_missing_row(self, tmp_path):
        """文件系统有/INDEX 无 → 缺行补全（编号+链接精确生成，说明列空）。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, [])  # 空 INDEX
        missing, orphans = m.check_index(m.local)
        # 01-overview 与 34-idle-refinement 在文件系统、不在 INDEX
        nums = [r.number for r in missing]
        assert "01" in nums and "34" in nums
        row34 = next(r for r in missing if r.number == "34")
        assert row34.link == "decisions/34-idle-refinement/34-idle-refinement.md"
        assert row34.desc == ""  # 说明列留空待人工


# ───────────────────────── T8-T9：trace 映射 ─────────────────────────

class TestTrace:
    def test_t8_plugins_prefix_normalized(self, tmp_path, monkeypatch):
        """trace 基准归一化：plugins/ca_assembler/xxx → 插件根相对存在。"""
        m, docs, _ = _mk(tmp_path)
        # 模拟决策文档 frontmatter
        dec = docs / "decisions" / "34-idle-refinement" / "34-idle-refinement.md"
        dec.write_text(
            "---\ntrace:\n  forward:\n    - ca/refinement.py\n    - plugins/ca_assembler/__init__.py\n"
            "  backward:\n    - ca/store.py\nsource_files:\n  - ca/refinement.py\n---\n# 34\n",
            encoding="utf-8",
        )
        broken = m.check_trace(m.local)
        # ca/refinement.py 与 __init__.py（归一化后）都应存在 → 无断链
        assert broken == []

    def test_t9_trace_broken_fixed(self, tmp_path, monkeypatch):
        """trace 断链修复后全部 resolve。"""
        m, docs, _ = _mk(tmp_path)
        dec = docs / "decisions" / "34-idle-refinement" / "34-idle-refinement.md"
        dec.write_text(
            "---\ntrace:\n  forward:\n    - ca/nonexistent_module.py\n"
            "source_files:\n  - ca/also_missing.py\n---\n# 34\n",
            encoding="utf-8",
        )
        broken = m.check_trace(m.local)
        assert len(broken) == 2
        # fix_trace 报告修复（真实代码文件不存在时不可自动修，返回 0 不崩）
        n = m.fix_trace(m.local, broken)
        assert n == 0  # 目标不存在 → 无自动修复对象，只报告


# ───────────────────────── T10-T11：changelog ─────────────────────────

class TestChangelog:
    def test_t10_draft_from_git(self, tmp_path, monkeypatch):
        """mock git log → draft 行含规范 commit，非规范进 manual。"""
        m, docs, _ = _mk(tmp_path)
        (docs / "changelog.md").write_text(
            "# changelog\n\n## 版本历史\n\n| 版本 | 日期 | 阶段 | 变更摘要 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n| **v7** | 2026-08-07 | x | y | z |\n",
            encoding="utf-8",
        )

        def fake_git(*args, **kw):
            return "abc1234|2026-08-08|feat, test: Step 9 文档维护\n" \
                   "def5678|2026-08-08|v7 手动标题无前缀\n"

        monkeypatch.setattr("subprocess.run", lambda *a, **k: type("R", (), {"stdout": fake_git(), "returncode": 0})())
        draft, manual = m.draft_changelog()
        assert any("abc1234" in line for line in draft)
        assert any("def5678" in line for line in manual)

    def test_t11_validate_structure(self, tmp_path):
        """列数不一致/日期格式错误/版本不同步 → 报告。"""
        m, docs, _ = _mk(tmp_path)
        (docs / "changelog.md").write_text(
            "# changelog\n\n## 版本历史\n\n| 版本 | 日期 | 阶段 | 变更摘要 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| **v7** | 2026-08-07 | x | y | z |\n"
            "| **v8** | 2026/08/08 | x | y |\n",  # 日期格式错 + 列数错
            encoding="utf-8",
        )
        issues = m.validate_changelog()
        assert len(issues) >= 2

    def test_e2_git_error_graceful(self, tmp_path, monkeypatch):
        """git log 异常 → draft 空列表不崩（评审 OC 补）。"""
        m, docs, _ = _mk(tmp_path)
        (docs / "changelog.md").write_text(
            "# changelog\n\n## 版本历史\n\n| 版本 | 日期 | 阶段 | 变更摘要 | 状态 |\n"
            "| --- | --- | --- | --- | --- |\n| **v7** | 2026-08-07 | x | y | z |\n",
            encoding="utf-8",
        )

        def raise_git(*args, **kw):
            raise OSError("git unavailable")

        monkeypatch.setattr("subprocess.run", raise_git)
        draft, manual = m.draft_changelog()
        assert draft == [] and manual == []


# ───────────────────────── T12：viking:// URI ─────────────────────────

class TestOvUris:
    def test_t12_uri_exists_and_missing(self, tmp_path):
        """OVFS 存在 → 有效；缺失 → 断链；OVFS 根不存在 → graceful。"""
        m, docs, ov = _mk(tmp_path)
        # 本地 docs 内引用 OV URI
        (docs / "ref.md").write_text(
            "见 [OV](viking://resources/projects/context-assembler/architecture/01-overview/01-overview.md) 与 "
            "[缺失](viking://resources/projects/context-assembler/ghost.md)\n",
            encoding="utf-8",
        )
        broken, unreachable = m.check_ov_uris()
        assert unreachable is False
        assert len(broken) == 1
        assert broken[0].link_target.endswith("ghost.md")

        # OVFS 不可达 → graceful
        m2 = dm.DocMaintenance(local_root=str(docs), ovfs_root=str(tmp_path / "no_such_ovfs"),
                               ov_project="projects/context-assembler")
        broken2, unreachable2 = m2.check_ov_uris()
        assert unreachable2 is True
        assert broken2 == []


# ───────────────────────── T13：wiki 残留 ─────────────────────────

class TestRemnants:
    def test_t13_report_only(self, tmp_path):
        """wiki/ 残留引用报告，不产生删除/修改。"""
        m, docs, _ = _mk(tmp_path)
        (docs / "changelog.md").write_text("→ [决策索引](docs/wiki/decisions/)\n", encoding="utf-8")
        remnants = m.report_remnants(m.local)
        assert len(remnants) == 1
        assert "wiki" in remnants[0].link_target
        # 文件未被修改
        assert "docs/wiki/decisions/" in (docs / "changelog.md").read_text(encoding="utf-8")


# ───────────────────────── T14：零 LLM ─────────────────────────

class TestZeroLlm:
    def test_t14_no_llm_calls(self, tmp_path):
        """DocReport.llm_calls 恒 0。"""
        m, docs, _ = _mk(tmp_path)
        _write_index(docs, ["| 01 | [概览](architecture/01-overview.md) | v6.0 | — | 说明 |"])
        report = m.run()
        assert report.llm_calls == 0


# ───────────────────────── T15-T16：双目标 ─────────────────────────

class TestDualTarget:
    def test_t15_dual_scan_independent(self, tmp_path):
        """本地与 OV 独立扫描、独立报告。"""
        m, docs, ov = _mk(tmp_path)
        _write_index(docs, ["| 01 | [概览](architecture/01-overview.md) | v6.0 | — | 说明 |"])
        report = m.run()
        assert len(report.local.broken_links) == 1  # 本地 INDEX 平铺断链
        assert len(report.ov.broken_links) == 1     # OV INDEX 平铺断链
        assert report.files_modified == []          # dry-run 默认

    def test_t16_ov_index_fix_report_only(self, tmp_path):
        """OV 侧 INDEX 修复可落盘；缺行仅报告不自动补。"""
        m, docs, ov = _mk(tmp_path)
        m.apply_ov = True
        broken = m.scan_links(m.ov)
        n = m.fix_links(m.ov, broken)
        assert n >= 1
        content = (ov / "INDEX.md").read_text(encoding="utf-8")
        assert "architecture/01-overview/01-overview.md" in content
        # OV 缺行（如 decisions/34）只报告不补
        missing, _ = m.check_index(m.ov)
        assert any(r.number == "34" for r in missing)


# ───────────────────────── T17：代码链接 ─────────────────────────

class TestCodeLinks:
    def test_t17_code_comment_links(self, tmp_path):
        """代码注释/文档字符串中文档引用 → 存在性校验；运行时路径逻辑不修改。"""
        m, docs, _ = _mk(tmp_path)
        # 用迷你代码目录模拟：注释引用（应校验）+ 运行时字符串（应排除）
        code = tmp_path / "code"
        code.mkdir()
        (code / "mod.py").write_text(
            '"""\n设计决策: viking://resources/projects/context-assembler/architecture/01-overview/01-overview.md\n"""\n'
            'def f():\n    # 运行时路径逻辑，排除\n    return "docs/architecture/"\n',
            encoding="utf-8",
        )
        # 先建 OV 目标（构造存在 → 0 断链）
        ov = tmp_path / "ovfs" / "projects" / "context-assembler"
        (ov / "architecture" / "01-overview").mkdir(parents=True, exist_ok=True)
        (ov / "architecture" / "01-overview" / "01-overview.md").write_text("# x\n", encoding="utf-8")
        broken = m.check_code_links([str(code)])
        assert broken == []  # docstring 引用存在 → 0 断链；函数体 docs/architecture/ 前缀被排除

    def test_t17b_code_link_count_and_no_store_change(self, tmp_path):
        """9.8 只校验注释/docstring 引用（AST），不把运行时字符串当断链，返回计数精确。"""
        m, docs, _ = _mk(tmp_path)
        code = tmp_path / "code2"
        code.mkdir()
        (code / "store.py").write_text(
            '"""\n设计决策: viking://resources/projects/context-assembler/architecture/01-overview/01-overview.md\n"""\n'
            'def _source_to_ov_uri(source_file):\n'
            '    if source_file.startswith("docs/architecture/"):\n'
            '        return "x"\n'
            '    return ""\n',
            encoding="utf-8",
        )
        ov = tmp_path / "ovfs" / "projects" / "context-assembler"
        (ov / "architecture" / "01-overview").mkdir(parents=True, exist_ok=True)
        (ov / "architecture" / "01-overview" / "01-overview.md").write_text("# x\n", encoding="utf-8")
        broken = m.check_code_links([str(code)])
        # docstring 1 处引用（存在 → 不断链）；函数体运行时字符串 0 处被当作链接
        assert broken == []
        # 运行时字符串不应出现在 code_link 类中
        assert all(b.category != "code_link" for b in broken)


# ───────────────────────── T18-T19：basename 查找 ─────────────────────────

class TestBasenameLookup:
    def test_t18_double_error_link(self, tmp_path):
        """双重错误链接（层级+平铺）→ basename 全局查找修正。"""
        m, docs, _ = _mk(tmp_path)
        arch = docs / "architecture" / "01-overview"
        (arch / "11-l-stage.md").write_text(
            "见 [决策34](../decisions/34-idle-refinement.md)\n", encoding="utf-8"
        )
        # 扫描全部链接（非 INDEX）
        broken = []
        for b in m.scan_links(m.local):
            if "34-idle-refinement" in b.link_target:
                broken.append(b)
        assert len(broken) == 1
        assert broken[0].suggested == "../../decisions/34-idle-refinement/34-idle-refinement.md"

    def test_t19_multi_candidate_needs_human(self, tmp_path):
        """同名文件多处命中 → needs_human 不自动修。"""
        m, docs, _ = _mk(tmp_path)
        # 构造两个同名 ca-single.md
        (docs / "architecture" / "01-overview" / "ca-single.md").write_text("# a\n", encoding="utf-8")
        (docs / "decisions" / "34-idle-refinement" / "ca-single.md").write_text("# b\n", encoding="utf-8")
        (docs / "ref.md").write_text("[single](ca-single.md)\n", encoding="utf-8")
        broken = m.scan_links(m.local)
        hits = [b for b in broken if b.link_target.endswith("ca-single.md")]
        assert len(hits) == 1
        assert hits[0].category == "needs_human"
        assert hits[0].suggested == ""
