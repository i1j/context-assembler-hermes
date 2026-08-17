"""ca/doc_maintenance.py — 精炼轮 Step 9 文档维护（决策 43 §4.1 链接对齐）。

设计决策: 43-ov-wiki-mirror-sync
  viking://resources/projects/context-assembler/decisions/43-ov-wiki-mirror-sync/43-ov-wiki-mirror-sync.md

纯 L1 代码、零 LLM、零 token。双目标：本地 docs/（git 管理）+ OVFS 权威源。
7 子任务（9.1-9.8）：
  9.1 相对链接对齐（scan_links / fix_links）
  9.2 INDEX 维护（check_index / apply_index）
  9.3 changelog 维护（validate_changelog / draft_changelog，仅本地）
  9.4 trace/source_files 映射校验（check_trace / fix_trace，基准=插件根）
  9.5 viking:// URI 校验（check_ov_uris，只读）
  9.6 wiki/ 残留报告（report_remnants，只读）
  9.7 OV 侧对齐（DocTree(is_ov=True) 实例执行 9.1/9.2/9.4/9.6）
  9.8 代码注释链接校验（check_code_links，只读，AST 区分 docstring/注释 vs 运行时字符串）

错误语义：所有方法不抛异常（读失败/OV 不可达内部消化为报告项）；
apply 写文件用原子写（tempfile+mv），失败 → io_errors += 1，无 partial-write。

仅使用标准库：os / re / subprocess / ast / tokenize / tempfile / glob。
"""

from __future__ import annotations

import ast
import glob
import io
import os
import re
import subprocess
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

__all__ = [
    "DocMaintenance",
    "DocTree",
    "BrokenLink",
    "IndexRow",
    "DocReport",
    "DocTreeReport",
]

_OVFS_ROOT_DEFAULT = os.path.expanduser("~/.openviking/data/viking/default/resources")
_OV_PROJECT_DEFAULT = "projects/context-assembler"

_INDEX_ROW_RE = re.compile(
    r"^\| (\*{0,2})(\d+\w*)\*{0,2} \| \*{0,2}\[([^\]]+)\]\(([^)]+)\)\*{0,2} \| "
    r"(.*?) \| (.*?) \| (.*?) \|$"
)
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_OV_URI_RE = re.compile(r"viking://resources/([^\s\]\)\"']+)")
_LOCAL_REF_RE = re.compile(r"docs/(?:architecture|decisions|testing)/[^\s\]\)\"']+")
_VERSION_ROW_RE = re.compile(r"^\|\s*\*\*v[\w.\-]+\*\*\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRAILING_PUNCT = ".,;:，。；：、）)]}」』】"
_COMMIT_PREFIX_RE = re.compile(
    r"^(feat|fix|docs|test|chore|refactor|build|perf|style|ci|revert)"
    r"(?:,\s*(?:feat|fix|docs|test|chore|refactor|build|perf|style|ci|revert))*:"
)


# ═══════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════

@dataclass
class BrokenLink:
    """一条断链/残留报告项。"""

    source_file: str = ""      # 含断链的文件（相对 DocTree 根）
    link_text: str = ""        # 原始链接/引用文本
    link_target: str = ""      # 解析后的目标路径
    suggested: str = ""        # 建议修复目标（"" = 不可自动修复）
    category: str = "relative"  # relative | trace | ov_uri | wiki_remnant | code_link | needs_human
    was_fixed: bool = False    # apply 后标记
    error_context: str = ""    # IO 错误上下文


@dataclass
class IndexRow:
    """INDEX.md 表格行（含文件系统枚举出的候选行）。"""

    number: str = ""           # "01" | "38a"
    name: str = ""             # 链接文本
    link: str = ""             # 链接目标
    version: str = ""
    doc_type: str = ""         # "arch" | "decision"
    desc: str = ""


@dataclass
class DocTree:
    """扫描/修复目标抽象（本地 docs/ 或 OVFS 根，双目标同构）。"""

    root: str = ""
    is_ov: bool = False
    ovfs_root: str = ""


@dataclass
class DocTreeReport:
    """单个目标的报告。"""

    broken_links: List[BrokenLink] = field(default_factory=list)
    index_missing: List[IndexRow] = field(default_factory=list)
    index_orphans: List[str] = field(default_factory=list)
    trace_broken: List[BrokenLink] = field(default_factory=list)
    remnants: List[BrokenLink] = field(default_factory=list)


@dataclass
class DocReport:
    """Step 9 全量报告。"""

    local: DocTreeReport = field(default_factory=DocTreeReport)
    ov: DocTreeReport = field(default_factory=DocTreeReport)
    total_before_fix: int = 0  # 修复前断链总数（local+ov）
    total_after_fix: int = 0   # 修复后（apply 后重扫）
    io_errors: int = 0
    changelog_issues: List[str] = field(default_factory=list)
    changelog_draft: List[str] = field(default_factory=list)
    changelog_manual: List[str] = field(default_factory=list)
    ov_unreachable: bool = False
    code_broken: List[BrokenLink] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)  # apply 模式落盘文件清单
    llm_calls: int = 0           # 恒 0（零 LLM 断言）


# ═══════════════════════════════════════════════════════════════
# DocMaintenance
# ═══════════════════════════════════════════════════════════════

class DocMaintenance:
    """精炼轮 Step 9 文档维护（决策 43 §4.1 链接对齐）。

    用法：
        dm = DocMaintenance(apply_local=not Config.REFINEMENT_DOC_DRY_RUN,
                            apply_ov=Config.REFINEMENT_DOC_OV_APPLY)
        report = dm.run()
    """

    def __init__(
        self,
        local_root: str = "docs",
        ovfs_root: str = _OVFS_ROOT_DEFAULT,
        ov_project: str = _OV_PROJECT_DEFAULT,
        apply_local: bool = False,
        apply_ov: bool = False,
    ) -> None:
        self.local_root = os.path.abspath(local_root)
        self.ovfs_root = os.path.abspath(ovfs_root) if ovfs_root else ""
        self.ov_project = ov_project
        self.apply_local = apply_local
        self.apply_ov = apply_ov
        self.local = DocTree(root=self.local_root, is_ov=False, ovfs_root=self.ovfs_root)
        ov_root = os.path.join(self.ovfs_root, ov_project) if self.ovfs_root else ""
        self.ov = DocTree(root=ov_root, is_ov=True, ovfs_root=self.ovfs_root)
        self.files_modified: List[str] = []
        self.io_errors: int = 0
        self._last_error_context: str = ""
        self._plugin_root = self._find_plugin_root()
        self._last_diff_summary: str = ""

    # ── 工具 ──

    def _find_plugin_root(self) -> str:
        """插件根（trace 基准）：优先取本模块所在包的上级（真实仓库），
        兜底取 docs/ 的上级。"""
        here = Path(__file__).resolve().parent  # .../ca/
        cand = here.parent
        if (cand / "ca").is_dir():
            return str(cand)
        parent = Path(self.local_root).parent
        if (parent / "ca").is_dir():
            return str(parent)
        return str(cand)

    def _iter_md_files(self, root: str):
        """遍历 root 下 .md 文件；跳过隐藏文件与 .abstract/.overview（OV 自动生成）。"""
        if not root or not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if not fn.endswith(".md"):
                    continue
                if fn.startswith(".") or ".abstract" in fn or ".overview" in fn:
                    continue
                yield os.path.join(dirpath, fn)

    def _basename_hits(self, root: str, basename: str) -> List[str]:
        """全局 basename 查找（排除 .abstract/.overview）。"""
        hits: List[str] = []
        try:
            pattern = os.path.join(root, "**", glob.escape(basename))
            for p in glob.glob(pattern, recursive=True):
                name = os.path.basename(p)
                if name.startswith(".") or ".abstract" in name or ".overview" in name:
                    continue
                hits.append(os.path.abspath(p))
        except Exception:
            pass
        return sorted(set(hits))

    def _atomic_write(self, path: str, content: str) -> bool:
        """原子写（tempfile + mv）；失败 → io_errors += 1，无 partial-write。"""
        try:
            d = os.path.dirname(path)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".ca_doc_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except Exception as exc:
            self.io_errors += 1
            self._last_error_context = str(exc)
            return False

    def _mark_modified(self, path: str) -> None:
        if path not in self.files_modified:
            self.files_modified.append(path)

    # ── 9.1 相对链接对齐 ──

    def _resolve_target(self, root: str, srcdir: str, target: str) -> Tuple[str, str]:
        """解析链接目标 → (状态, 建议)：
        valid=存在（跳过）；fixable=嵌套/basename 唯一命中（断链+suggested）；
        missing=未解析（needs_human）。"""
        resolved = os.path.normpath(os.path.join(srcdir, target))
        if os.path.exists(resolved):
            return "valid", ""
        if target.endswith(".md"):
            base_dir, base = os.path.split(target)
            cand = os.path.normpath(os.path.join(srcdir, base_dir, base[:-3], base))
            if os.path.exists(cand):
                return "fixable", os.path.relpath(cand, srcdir).replace(os.sep, "/")
            hits = self._basename_hits(root, os.path.basename(target))
            if len(hits) == 1:
                return "fixable", os.path.relpath(hits[0], srcdir).replace(os.sep, "/")
        return "missing", ""

    def scan_links(self, tree: DocTree) -> List[BrokenLink]:
        """扫描 tree 下所有 .md 的 markdown 链接，返回断链/残留清单。

        三级解析：存在 → 嵌套补全（path.md → path/path.md）→ basename 全局查找
        （唯一命中建议相对路径；多/零命中 needs_human）。wiki/ 引用单独标
        wiki_remnant（不自动修）。http/https/mailto/viking:///# 跳过。
        """
        broken: List[BrokenLink] = []
        root = tree.root
        if not root or not os.path.isdir(root):
            return broken
        try:
            for path in self._iter_md_files(root):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                except OSError:
                    continue
                rel = os.path.relpath(path, root)
                srcdir = os.path.dirname(path)
                for m in _LINK_RE.finditer(content):
                    raw = m.group(1).strip()
                    if not raw or raw.startswith(
                        ("<", "http://", "https://", "mailto:", "viking://", "#", "data:")
                    ):
                        continue
                    target = raw.split("#", 1)[0].split("?", 1)[0].strip()
                    if not target:
                        continue
                    if "wiki/" in target or target.startswith("docs/wiki"):
                        broken.append(BrokenLink(
                            source_file=rel, link_text=raw, link_target=target,
                            suggested="", category="wiki_remnant"))
                        continue
                    status, suggested = self._resolve_target(root, srcdir, target)
                    if status == "valid":
                        continue
                    broken.append(BrokenLink(
                        source_file=rel, link_text=raw, link_target=target,
                        suggested=suggested,
                        category="relative" if status == "fixable" else "needs_human"))
        except Exception:
            pass  # 内部消化，不抛异常
        return broken

    def fix_links(self, tree: DocTree, broken: List[BrokenLink]) -> int:
        """apply：按 suggested 修复断链（跳过 needs_human/wiki_remnant 等）。"""
        fixed = 0
        by_file: dict = {}
        for b in broken:
            if b.category not in ("relative", "trace"):
                continue
            if not b.suggested:
                continue
            by_file.setdefault(b.source_file, []).append(b)
        for rel, items in by_file.items():
            path = os.path.join(tree.root, rel)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError as exc:
                self.io_errors += 1
                for b in items:
                    b.was_fixed = False
                    b.error_context = str(exc)
                continue
            new_content = content
            changed = False
            seen: set = set()
            for b in items:
                if b.link_text in seen:
                    continue
                seen.add(b.link_text)
                old = "](" + b.link_text + ")"
                new = "](" + b.suggested + ")"
                if old in new_content:
                    new_content = new_content.replace(old, new)
                    b.was_fixed = True
                    changed = True
                    fixed += 1
            if changed:
                if self._atomic_write(path, new_content):
                    self._mark_modified(path)
        return fixed

    # ── 9.2 INDEX 维护 ──

    def _enumerate_index_dirs(self, root: str) -> List[IndexRow]:
        """枚举 architecture/ 与 decisions/ 下 NN-*（含 .md/ 后缀目录）双形态。"""
        rows: List[IndexRow] = []
        for section, doc_type in (("architecture", "arch"), ("decisions", "decision")):
            sdir = os.path.join(root, section)
            if not os.path.isdir(sdir):
                continue
            try:
                names = sorted(os.listdir(sdir))
            except OSError:
                continue
            for name in names:
                m = re.match(r"^(\d+\w*)-", name)
                if not m:
                    continue
                if not os.path.isdir(os.path.join(sdir, name)):
                    continue
                number = m.group(1)
                if name.endswith(".md"):
                    link = f"{section}/{name}/{name}"
                else:
                    link = f"{section}/{name}/{name}.md"
                rows.append(IndexRow(number=number, name=name, link=link,
                                     version="", doc_type=doc_type, desc=""))
        return rows

    def check_index(self, tree: DocTree) -> Tuple[List[IndexRow], List[str]]:
        """(缺行, 孤儿引用)：文件系统有/INDEX 无 → 缺行；INDEX 有/文件系统无 → 孤儿。"""
        missing: List[IndexRow] = []
        orphans: List[str] = []
        root = tree.root
        if not root or not os.path.isdir(root):
            return missing, orphans
        rows: List[IndexRow] = []
        index_path = os.path.join(root, "INDEX.md")
        if os.path.isfile(index_path):
            try:
                with open(index_path, "r", encoding="utf-8") as f:
                    for line in f:
                        m = _INDEX_ROW_RE.match(line.strip())
                        if m:
                            rows.append(IndexRow(number=m.group(2), name=m.group(3),
                                                 link=m.group(4), version=m.group(5),
                                                 doc_type=m.group(6), desc=m.group(7)))
            except OSError:
                pass
        fs_rows = self._enumerate_index_dirs(root)
        fs_by_num = {r.number: r for r in fs_rows}
        idx_by_num = {r.number: r for r in rows}
        for num in sorted(fs_by_num):
            if num not in idx_by_num:
                missing.append(fs_by_num[num])
        for row in sorted(rows, key=lambda r: r.number):
            target = row.link.split("#", 1)[0]
            if not target:
                continue
            status, _ = self._resolve_target(root, root, target)
            if status == "missing":
                orphans.append(row.link)
        return missing, orphans

    def apply_index(self, tree: DocTree, missing: List[IndexRow]) -> bool:
        """本地：缺行补全（按编号去重）；OV：只报告不补（返回 False）。"""
        if not missing:
            return True
        if tree.is_ov:
            return False
        index_path = os.path.join(tree.root, "INDEX.md")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                lines = f.read().split("\n")
        except OSError as exc:
            self.io_errors += 1
            return False
        by_section: dict = {}
        for row in missing:
            section = "architecture" if row.link.startswith("architecture/") else "decisions"
            by_section.setdefault(section, []).append(row)
        new_lines = list(lines)
        for section, rows in by_section.items():
            anchor = self._index_insert_after(new_lines, section)
            insert = []
            for row in sorted(rows, key=lambda r: r.number):
                insert.append(
                    f"| {row.number} | [{row.name}]({row.link}) | {row.version} | "
                    f"{row.doc_type} | {row.desc} |"
                )
            new_lines[anchor + 1:anchor + 1] = insert
        if self._atomic_write(index_path, "\n".join(new_lines)):
            self._mark_modified(index_path)
            return True
        return False

    def _index_insert_after(self, lines: List[str], section: str) -> int:
        """定位 section 表（architecture/decisions）最后一行之后。"""
        prefix = section + "/"
        last_row = -1
        for i, line in enumerate(lines):
            m = _INDEX_ROW_RE.match(line.strip())
            if m and m.group(4).startswith(prefix):
                last_row = i
        if last_row >= 0:
            return last_row
        # 兜底：section 表头之后（表头 + 分隔行）
        for i, line in enumerate(lines):
            if line.startswith("## ") and section in line:
                return min(i + 2, len(lines) - 1)
        return len(lines) - 1

    # ── 9.4 trace / source_files 映射校验 ──

    def _extract_trace_paths(self, content: str) -> List[str]:
        """解析 frontmatter 的 trace.forward/backward 与 source_files 路径。"""
        paths: List[str] = []
        m = re.match(r"^---\s*\n(.*?)\n---\s*", content, re.DOTALL)
        if not m:
            return paths
        lines = m.group(1).split("\n")
        in_trace = False
        in_source_list = False
        for line in lines:
            if line[:1] in (" ", "\t"):
                if in_trace or in_source_list:
                    m3 = re.match(r"^\s*-\s+(.+)$", line)
                    if m3:
                        paths.append(m3.group(1).strip())
                continue
            s = line.strip()
            if re.match(r"^trace\s*:", s):
                in_trace = True
                in_source_list = False
                continue
            if re.match(r"^source_files\s*:", s):
                in_trace = False
                in_source_list = True
                rest = s.split(":", 1)[1].strip()
                if rest.startswith("["):
                    for item in re.split(r"\s*,\s*", rest.strip("[]").strip()):
                        item = item.strip().strip("\"'")
                        if item:
                            paths.append(item)
                    in_source_list = False
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", s):
                in_trace = False
                in_source_list = False
        return paths

    def _normalize_trace_path(self, p: str) -> str:
        """基准归一化：plugins/ca_assembler/xxx → xxx（插件根相对）。"""
        p = p.strip().strip("`\"'")
        if p.startswith("plugins/ca_assembler/"):
            return p[len("plugins/ca_assembler/"):]
        if p.startswith("./"):
            return p[2:]
        return p

    def check_trace(self, tree: DocTree) -> List[BrokenLink]:
        """校验决策/架构文档 frontmatter 的代码路径相对插件根存在。"""
        broken: List[BrokenLink] = []
        root = tree.root
        if not root or not os.path.isdir(root):
            return broken
        for path in self._iter_md_files(root):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            paths = self._extract_trace_paths(content)
            if not paths:
                continue
            rel = os.path.relpath(path, root)
            for p in paths:
                if not self._looks_like_path(p):
                    continue
                norm = self._normalize_trace_path(p)
                if not norm:
                    continue
                full = os.path.join(self._plugin_root, norm)
                if os.path.exists(full):
                    continue
                suggested = ""
                hits = self._basename_hits(self._plugin_root, os.path.basename(norm))
                if len(hits) == 1:
                    suggested = os.path.relpath(hits[0], self._plugin_root).replace(os.sep, "/")
                broken.append(BrokenLink(source_file=rel, link_text=p, link_target=p,
                                         suggested=suggested, category="trace"))
        return broken

    def _looks_like_path(self, p: str) -> bool:
        """trace 条目是否像文件路径（排除 全代码库 等语义值）。"""
        p = p.strip().strip("`\"'")
        if not p or p.startswith("viking://"):
            return False
        return "/" in p or "." in os.path.basename(p)

    def fix_trace(self, tree: DocTree, broken: List[BrokenLink]) -> int:
        """apply：trace 断链按 suggested 修复（无命中对象时返回 0 只报告）。"""
        fixed = 0
        by_file: dict = {}
        for b in broken:
            if b.category != "trace" or not b.suggested:
                continue
            by_file.setdefault(b.source_file, []).append(b)
        for rel, items in by_file.items():
            path = os.path.join(tree.root, rel)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                self.io_errors += 1
                continue
            new_content = content
            changed = False
            for b in items:
                if b.link_text in new_content:
                    new_content = new_content.replace(b.link_text, b.suggested)
                    b.was_fixed = True
                    changed = True
                    fixed += 1
            if changed and self._atomic_write(path, new_content):
                self._mark_modified(path)
        return fixed

    # ── 9.3 changelog（仅本地）──

    def validate_changelog(self) -> List[str]:
        """结构校验：表头/列数/日期格式/版本表 vs 详细段不同步。"""
        issues: List[str] = []
        path = os.path.join(self.local_root, "changelog.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return ["changelog.md 不可读"]
        lines = content.split("\n")
        in_table = False
        table_versions: List[str] = []
        for i, line in enumerate(lines):
            if line.startswith("| 版本 | 日期 |"):
                in_table = True
                continue
            if not in_table:
                continue
            if line.startswith("|") and re.match(r"^\|\s*-{2,}", line):
                continue
            if line.startswith("|") and line.strip():
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) != 5:
                    issues.append(f"changelog 第 {i + 1} 行列数不一致（{len(cells)}/5）")
                else:
                    mver = re.match(r"\*\*(v[\w.\-]+)\*\*", cells[0])
                    if mver:
                        table_versions.append(mver.group(1))
                if len(cells) >= 2 and not _DATE_RE.match(cells[1]):
                    issues.append(f"changelog 第 {i + 1} 行日期格式错误（{cells[1]}）")
            else:
                in_table = False
        detail_versions = set(re.findall(r"^###\s+(v[\w.\-]+)\b", content, re.MULTILINE))
        table_set = set(table_versions)
        if table_set and detail_versions and not table_set.issubset(detail_versions):
            issues.append("changelog 版本表与详细段不同步（版本表有详细段缺失的版本）")
        return issues

    def draft_changelog(self) -> Tuple[List[str], List[str]]:
        """git log --since=<最后条目日期> → (规范 draft 行, 非规范 manual 清单)。

        draft 模式不落盘：输出报告，版本号标 _pending_ 由人工填。
        """
        draft: List[str] = []
        manual: List[str] = []
        path = os.path.join(self.local_root, "changelog.md")
        last_date = ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    m = _VERSION_ROW_RE.match(line)
                    if m:
                        last_date = m.group(1)
        except OSError:
            return draft, manual
        try:
            args = ["git", "log", "--pretty=format:%h|%ad|%s", "--date=short"]
            if last_date:
                args.append("--since=" + last_date)
            res = subprocess.run(
                args, capture_output=True, text=True, timeout=60, cwd=self._plugin_root
            )
            if res.returncode != 0:
                return draft, manual
            out = res.stdout or ""
        except Exception:
            return draft, manual  # git 不可用 → graceful
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            h, d, subject = parts[0], parts[1], parts[2]
            if _COMMIT_PREFIX_RE.match(subject.strip()):
                draft.append(f"| _pending_ | {d} | 开发 | {subject.strip()} | {h} |")
            else:
                manual.append(line)
        return draft, manual

    # ── 9.5 viking:// URI 校验（只读）──

    def check_ov_uris(self) -> Tuple[List[BrokenLink], bool]:
        """本地 docs/ 内 viking:// URI → OVFS 存在性校验。

        OVFS 根不存在/scandir 失败 → ([], True) 永不 raise。
        """
        if not self.ovfs_root or not os.path.isdir(self.ovfs_root):
            return [], True
        broken: List[BrokenLink] = []
        root = self.local_root
        for path in self._iter_md_files(root):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            for m in _OV_URI_RE.finditer(content):
                uri = m.group(1).split("#", 1)[0].split("?", 1)[0].rstrip(_TRAILING_PUNCT)
                if not uri or uri.endswith("/"):
                    continue
                fs = os.path.normpath(os.path.join(self.ovfs_root, uri))
                if not os.path.exists(fs):
                    broken.append(BrokenLink(source_file=rel, link_text=m.group(0),
                                             link_target=uri, suggested="", category="ov_uri"))
        return broken, False

    # ── 9.6 wiki/ 残留报告（只读）──

    def report_remnants(self, tree: DocTree) -> List[BrokenLink]:
        """扫描指向 docs/wiki/ 或 wiki/ 的链接/裸路径 → 报告（不删不改）。"""
        remnants: List[BrokenLink] = []
        root = tree.root
        if not root or not os.path.isdir(root):
            return remnants
        for path in self._iter_md_files(root):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            found: List[str] = []
            for m in _LINK_RE.finditer(content):
                target = m.group(1).strip().split("#", 1)[0]
                if "wiki/" in target or target.startswith("docs/wiki"):
                    found.append(target)
            for m in re.finditer(r"docs/wiki[^\s`\"')\]]*", content):
                found.append(m.group(0))
            seen: set = set()
            for text in found:
                if text in seen:
                    continue
                seen.add(text)
                remnants.append(BrokenLink(source_file=rel, link_text=text, link_target=text,
                                           suggested="", category="wiki_remnant"))
        return remnants

    # ── 9.8 代码注释链接校验（只读）──

    def _extract_code_refs(self, src: str) -> List[Tuple[str, str]]:
        """AST：docstring 中文档引用；tokenize：注释中文档引用。
        函数体内运行时字符串（非 docstring 语句）不参与（排除逻辑）。"""
        refs: List[Tuple[str, str]] = []
        texts: List[str] = []
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node)
                    if doc:
                        texts.append(doc)
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    texts.append(tok.string)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
        for text in texts:
            for m in _OV_URI_RE.finditer(text):
                ref = m.group(1).split("#", 1)[0].split("?", 1)[0].rstrip(_TRAILING_PUNCT)
                if ref and not ref.endswith("/") and "." in os.path.basename(ref):
                    refs.append((ref, "ov"))
            for m in _LOCAL_REF_RE.finditer(text):
                ref = m.group(0).split("#", 1)[0].split("?", 1)[0].rstrip(_TRAILING_PUNCT)
                if ref and not ref.endswith("/"):
                    refs.append((ref, "local"))
        return refs

    def check_code_links(self, code_roots: List[str]) -> List[BrokenLink]:
        """校验 ca/ + scripts/ 注释/docstring 中的 OV URI 与 docs/ 路径存在性。"""
        broken: List[BrokenLink] = []
        ov_ok = bool(self.ovfs_root) and os.path.isdir(
            os.path.join(self.ovfs_root, self.ov_project))
        for root in code_roots or []:
            base = root if os.path.isabs(root) else os.path.join(self._plugin_root, root)
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in sorted(filenames):
                    if not fn.endswith(".py"):
                        continue
                    path = os.path.join(dirpath, fn)
                    rel = os.path.relpath(path, self._plugin_root)
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            src = f.read()
                    except OSError:
                        continue
                    for ref, kind in self._extract_code_refs(src):
                        if kind == "ov":
                            if not ov_ok:
                                continue
                            fs = os.path.normpath(os.path.join(self.ovfs_root, ref))
                        else:
                            fs = os.path.normpath(
                                os.path.join(self.local_root, ref[len("docs/"):]))
                        if not os.path.exists(fs):
                            broken.append(BrokenLink(source_file=rel, link_text=ref,
                                                     link_target=ref, suggested="",
                                                     category="code_link"))
        return broken

    # ── 9.7 汇总执行 ──

    def run(self) -> DocReport:
        """全量执行 7 子任务（本地 + OV），默认 dry-run 只报告。"""
        local_rep = DocTreeReport()
        ov_rep = DocTreeReport()
        ov_has_root = bool(self.ov.root) and os.path.isdir(self.ov.root)

        # ── 本地 9.1 ──
        try:
            local_broken = self.scan_links(self.local)
        except Exception:
            local_broken = []
        local_rep.broken_links = local_broken
        before = len(local_broken)
        if ov_has_root:
            try:
                before += len(self.scan_links(self.ov))
            except Exception:
                pass
        if self.apply_local:
            try:
                self.fix_links(self.local, local_broken)
            except Exception:
                pass

        # ── 本地 9.2 ──
        try:
            missing, orphans = self.check_index(self.local)
        except Exception:
            missing, orphans = [], []
        local_rep.index_missing = missing
        local_rep.index_orphans = orphans
        if self.apply_local and missing:
            try:
                self.apply_index(self.local, missing)
            except Exception:
                pass

        # ── 本地 9.4 ──
        try:
            trace_broken = self.check_trace(self.local)
        except Exception:
            trace_broken = []
        local_rep.trace_broken = trace_broken
        if self.apply_local and trace_broken:
            try:
                self.fix_trace(self.local, trace_broken)
            except Exception:
                pass

        # ── 本地 9.6 ──
        try:
            local_rep.remnants = self.report_remnants(self.local)
        except Exception:
            pass

        # ── 本地 9.3 changelog ──
        try:
            changelog_issues = self.validate_changelog()
        except Exception:
            changelog_issues = []
        try:
            changelog_draft, changelog_manual = self.draft_changelog()
        except Exception:
            changelog_draft, changelog_manual = [], []

        # ── OV 9.7（9.1/9.2/9.4/9.6；缺行只报告）──
        if ov_has_root:
            try:
                ov_broken = self.scan_links(self.ov)
            except Exception:
                ov_broken = []
            ov_rep.broken_links = ov_broken
            if self.apply_ov:
                try:
                    self.fix_links(self.ov, ov_broken)
                except Exception:
                    pass
            try:
                ov_missing, ov_orphans = self.check_index(self.ov)
            except Exception:
                ov_missing, ov_orphans = [], []
            ov_rep.index_missing = ov_missing
            ov_rep.index_orphans = ov_orphans
            try:
                ov_rep.trace_broken = self.check_trace(self.ov)
            except Exception:
                pass
            if self.apply_ov and ov_rep.trace_broken:
                try:
                    self.fix_trace(self.ov, ov_rep.trace_broken)
                except Exception:
                    pass
            try:
                ov_rep.remnants = self.report_remnants(self.ov)
            except Exception:
                pass

        # ── 9.5 viking:// URI（只读）──
        try:
            _, ov_unreachable = self.check_ov_uris()
        except Exception:
            ov_unreachable = True

        # ── 9.8 代码链接（只读）──
        try:
            code_broken = self.check_code_links(["ca", "scripts"])
        except Exception:
            code_broken = []

        try:
            after = len(self.scan_links(self.local))
        except Exception:
            after = len(local_broken)
        if ov_has_root:
            try:
                after += len(self.scan_links(self.ov))
            except Exception:
                pass

        return DocReport(
            local=local_rep,
            ov=ov_rep,
            total_before_fix=before,
            total_after_fix=after,
            io_errors=self.io_errors,
            changelog_issues=changelog_issues,
            changelog_draft=changelog_draft,
            changelog_manual=changelog_manual,
            ov_unreachable=ov_unreachable,
            code_broken=code_broken,
            files_modified=list(self.files_modified),
            llm_calls=0,
        )

    # ── 集成验收辅助 ──

    def diff_summary(self) -> str:
        """干版 git diff 摘要字符串（集成验收用）。"""
        rels = sorted(set(os.path.relpath(f, self._plugin_root) for f in self.files_modified))
        if not rels:
            return ""
        try:
            res = subprocess.run(
                ["git", "-C", self._plugin_root, "diff", "--stat", "--", *rels],
                capture_output=True, text=True, timeout=30,
            )
            out = (res.stdout or "").strip()
            if out:
                self._last_diff_summary = out
                return out
        except Exception:
            pass
        self._last_diff_summary = "\n".join(rels)
        return self._last_diff_summary
