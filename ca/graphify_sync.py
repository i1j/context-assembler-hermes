"""graphify 增量同步（v6.5.3 theme 适配版）。

v6.5 themes 重构后，原 `_graphify_incremental`（读旧表 topic_wiki）失效。
本模块适配 themes 表：每次 theme create/merge 后增量同步节点 + merged_into 边
到 graphify-out/graph.json（与 scripts/wiki_to_graph.py 全量格式一致，幂等）。

节点: theme_{theme_id}（label: [知识] {title}）
边:   topic_{session_id}_S{strand_id} → theme_{theme_id}（relation: merged_into）
"""
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

NODE_PREFIX = "[知识]"


def _theme_rows(theme_ids: list[int], db_path: Optional[Path]):
    """从 themes 表读取待同步的 theme（theme_id, title, source_strands）。"""
    try:
        from ca.store import _get_topic_conn
    except ImportError:  # 独立运行（无插件上下文）
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            f"SELECT theme_id, title, source_strands FROM themes "
            f"WHERE theme_id IN ({','.join('?' for _ in theme_ids)})",
            theme_ids,
        ).fetchall()
        conn.close()
        return rows
    conn = _get_topic_conn(db_path)
    rows = conn.execute(
        f"SELECT theme_id, title, source_strands FROM themes "
        f"WHERE theme_id IN ({','.join('?' for _ in theme_ids)})",
        theme_ids,
    ).fetchall()
    return rows


def sync_themes_to_graph(
    theme_ids: list[int],
    graph_path: Path,
    db_path: Optional[Path] = None,
) -> Tuple[int, int]:
    """将 theme 增量同步到 graph.json（幂等）。

    Args:
        theme_ids: 本次 create/merge 涉及的 theme_id 列表
        graph_path: graph.json 路径
        db_path: ca_topics.db 路径（None → 默认 profile DB）

    Returns:
        (added_nodes, added_links)
    """
    if not theme_ids:
        return (0, 0)
    graph_path = Path(graph_path)
    if not graph_path.exists():
        logger.debug("[CA_GRAPH] No graph.json yet, skipping incremental sync")
        return (0, 0)

    rows = _theme_rows(theme_ids, db_path)
    if not rows:
        return (0, 0)

    new_nodes: list[dict] = []
    new_links: list[dict] = []
    for theme_id, title, src_json in rows:
        title = (title or "").strip()
        label = f"{NODE_PREFIX} {title[:100]}" if title else f"{NODE_PREFIX} Theme {theme_id}"
        nid = f"theme_{theme_id}"
        new_nodes.append({
            "id": nid,
            "label": label,
            "norm_label": label.lower().replace(" ", "_")[:64],
            "file_type": "knowledge",
            "source_file": "ca_topics.db",
            "source_location": f"themes.theme_id={theme_id}",
            "_origin": "theme_merge",
            "community": 0,
        })
        # source_strands: {"session_id": [strand_id, ...]} → merged_into 边
        try:
            sources = json.loads(src_json) if src_json else {}
        except (json.JSONDecodeError, TypeError):
            sources = {}
        if isinstance(sources, dict):
            for sid, strand_ids in sources.items():
                for strand_id in strand_ids or []:
                    topic_nid = f"topic_{sid}_S{strand_id}"
                    new_links.append({
                        "source": topic_nid,
                        "target": nid,
                        "relation": "merged_into",
                        "confidence": "HIGH",
                        "confidence_score": 0.95,
                        "source_file": "themes",
                        "source_location": f"source_strands:{sid}:{strand_id}",
                        "weight": 1.0,
                    })

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)

    existing_ids = {n["id"] for n in graph.get("nodes", [])}
    existing_links = {(l["source"], l["target"], l.get("relation", ""))
                      for l in graph.get("links", [])}

    added_nodes = 0
    added_links = 0
    for node in new_nodes:
        if node["id"] not in existing_ids:
            graph.setdefault("nodes", []).append(node)
            existing_ids.add(node["id"])
            added_nodes += 1
    for link in new_links:
        key = (link["source"], link["target"], link["relation"])
        if key not in existing_links:
            graph.setdefault("links", []).append(link)
            existing_links.add(key)
            added_links += 1

    if added_nodes or added_links:
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    logger.info("[CA_GRAPH] theme sync: +%d nodes, +%d links", added_nodes, added_links)
    return (added_nodes, added_links)


# ═══════════════════════════════════════════════════════════
# v7 (决策 38): 共现边入图（reality/theme 间 co_occurs_with 边）
# ═══════════════════════════════════════════════════════════


def _theme_title_map(theme_ids: list, db_path: Optional[Path]) -> dict:
    """从 themes 表读 title（节点 label 用，v6.5 兼容）。"""
    if not theme_ids:
        return {}
    try:
        rows = _theme_rows(theme_ids, db_path)
    except Exception:
        return {}
    return {r[0]: (r[1] or "") for r in rows}


def _reality_rows(reality_ids: list[int], db_path: Optional[Path]):
    """从 realities 表读待同步的 reality（reality_id, name, source_strands）。"""
    if not reality_ids:
        return []
    try:
        from ca.store import _get_topic_conn
    except ImportError:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            f"SELECT reality_id, name, source_strands FROM realities "
            f"WHERE reality_id IN ({','.join('?' for _ in reality_ids)})",
            reality_ids).fetchall()
        conn.close()
        return rows
    conn = _get_topic_conn(db_path)
    return conn.execute(
        f"SELECT reality_id, name, source_strands FROM realities "
        f"WHERE reality_id IN ({','.join('?' for _ in reality_ids)})",
        reality_ids).fetchall()


def _reality_title_map(reality_ids: list, db_path: Optional[Path]) -> dict:
    """从 realities 表读 name（节点 label 用，决策 41）。"""
    if not reality_ids:
        return {}
    try:
        rows = _reality_rows(reality_ids, db_path)
    except Exception:
        return {}
    return {r[0]: (r[1] or "") for r in rows}


def sync_realities_to_graph(
    reality_ids: list[int],
    graph_path: Path,
    db_path: Optional[Path] = None,
) -> Tuple[int, int]:
    """将 reality 增量同步到 graph.json（决策 41，幂等）。

    节点: reality_{reality_id}（label: [知识] {name}）
    边:   topic_{session_id}_S{strand_id} → reality_{reality_id}（merged_into）
    与 sync_cooccurrences_to_graph 共享节点命名 reality_{id}。
    """
    if not reality_ids:
        return (0, 0)
    graph_path = Path(graph_path)
    if not graph_path.exists():
        logger.debug("[CA_GRAPH] No graph.json yet, skipping reality sync")
        return (0, 0)

    rows = _reality_rows(reality_ids, db_path)
    if not rows:
        return (0, 0)

    new_nodes: list[dict] = []
    new_links: list[dict] = []
    for rid, name, src_json in rows:
        name = (name or "").strip()
        label = f"{NODE_PREFIX} {name[:100]}" if name else f"{NODE_PREFIX} Reality {rid}"
        nid = f"reality_{rid}"
        new_nodes.append({
            "id": nid,
            "label": label,
            "norm_label": label.lower().replace(" ", "_")[:64],
            "file_type": "knowledge",
            "source_file": "ca_topics.db",
            "source_location": f"realities.reality_id={rid}",
            "_origin": "reality_merge",
            "community": 0,
        })
        # source_strands: {"session_id": [strand_id, ...]} → merged_into 边
        try:
            sources = json.loads(src_json) if src_json else {}
        except (json.JSONDecodeError, TypeError):
            sources = {}
        if isinstance(sources, dict):
            for sid, strand_ids in sources.items():
                for strand_id in strand_ids or []:
                    topic_nid = f"topic_{sid}_S{strand_id}"
                    new_links.append({
                        "source": topic_nid,
                        "target": nid,
                        "relation": "merged_into",
                        "confidence": "HIGH",
                        "confidence_score": 0.95,
                        "source_file": "realities",
                        "source_location": f"source_strands:{sid}:{strand_id}",
                        "weight": 1.0,
                    })

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)

    existing_ids = {n["id"] for n in graph.get("nodes", [])}
    existing_links = {(l["source"], l["target"], l.get("relation", ""))
                      for l in graph.get("links", [])}

    added_nodes = 0
    added_links = 0
    for node in new_nodes:
        if node["id"] not in existing_ids:
            graph.setdefault("nodes", []).append(node)
            existing_ids.add(node["id"])
            added_nodes += 1
    for link in new_links:
        key = (link["source"], link["target"], link["relation"])
        if key not in existing_links:
            graph.setdefault("links", []).append(link)
            existing_links.add(key)
            added_links += 1

    if added_nodes or added_links:
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    logger.info("[CA_GRAPH] reality sync: +%d nodes, +%d links", added_nodes, added_links)
    return (added_nodes, added_links)


def sync_cooccurrences_to_graph(
    edges: dict,
    graph_path: Path,
    db_path: Optional[Path] = None,
    node_titles: Optional[dict] = None,
) -> Tuple[int, int]:
    """共现边入图（决策 38 v7 / 41）：reality_a → reality_b（relation: co_occurs_with）。

    - 节点不存在则补建（label 从 realities 表 name 读，node_titles 可覆盖）
    - 已存在的 co_occurs_with 边更新 weight（共现次数增长，非幂等追加）
    - 与 sync_realities_to_graph 共享节点命名 reality_{id}

    Returns:
        (added_nodes, changed_links)  # changed = 新增边 + 权重更新的边
    """
    if not edges:
        return (0, 0)
    graph_path = Path(graph_path)
    if not graph_path.exists():
        logger.debug("[CA_GRAPH] No graph.json yet, skipping co-occurrence sync")
        return (0, 0)
    if node_titles is None:
        node_titles = _reality_title_map(
            [x for pair in edges for x in pair], db_path)

    with open(graph_path, encoding="utf-8") as f:
        graph = json.load(f)

    existing_ids = {n["id"] for n in graph.get("nodes", [])}
    existing_links = {(l["source"], l["target"], l.get("relation", ""))
                      for l in graph.get("links", [])}

    added_nodes = 0
    changed_links = 0
    changed = False
    for (a, b), w in sorted(edges.items()):
        na, nb = f"reality_{a}", f"reality_{b}"
        for nid, tid in ((na, a), (nb, b)):
            if nid not in existing_ids:
                title = (node_titles.get(tid) or "").strip()
                label = (f"{NODE_PREFIX} {title[:100]}"
                         if title else f"{NODE_PREFIX} Reality {tid}")
                graph.setdefault("nodes", []).append({
                    "id": nid,
                    "label": label,
                    "norm_label": label.lower().replace(" ", "_")[:64],
                    "file_type": "knowledge",
                    "source_file": "ca_topics.db",
                    "source_location": f"cooccurrence_events.reality_id={tid}",
                    "_origin": "cooccurrence",
                    "community": 0,
                })
                existing_ids.add(nid)
                added_nodes += 1
                changed = True
        key = (na, nb, "co_occurs_with")
        if key in existing_links:
            for link in graph.get("links", []):
                if (link.get("source"), link.get("target"),
                        link.get("relation")) == key:
                    if link.get("weight") != float(w):
                        link["weight"] = float(w)
                        changed_links += 1
                        changed = True
                    break
        else:
            graph.setdefault("links", []).append({
                "source": na,
                "target": nb,
                "relation": "co_occurs_with",
                "confidence": "HIGH",
                "confidence_score": 0.95,
                "source_file": "cooccurrence_events",
                "source_location": f"reality_{a}xreality_{b}",
                "weight": float(w),
            })
            existing_links.add(key)
            changed_links += 1
            changed = True

    if changed:
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)

    logger.info("[CA_GRAPH] cooccurrence sync: +%d nodes, %d links changed",
                added_nodes, changed_links)
    return (added_nodes, changed_links)
