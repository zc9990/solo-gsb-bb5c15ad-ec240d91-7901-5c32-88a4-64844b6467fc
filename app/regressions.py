"""Historical regression comparison between two persisted A/B experiments.

Given the ids of two previously run experiments, the **same named rule
profiles** are paired across the two runs and the **same queries** (after
stripping leading/trailing whitespace) are compared per profile.  Each paired
result is classified as:

- ``improved``      -- status moved towards an automatic hit
                       (``no_match`` → ``needs_review`` → ``auto_matched``);
- ``regressed``     -- status moved away from an automatic hit, or the status
                       stayed the same while the preferred address changed;
- ``score_only``    -- identical status and preferred address, but the score
                       changed;
- ``unchanged``     -- identical status, preferred address and score.

Alignment is strict: every profile name must exist in both experiments and the
two experiments must contain the same stripped query set.  Missing profiles,
missing/extra/duplicate queries are all reported item-by-item and, if any such
problem exists, no comparison is produced.

The two experiments may have been run against different address-table
snapshots.  When the snapshot digests differ the rows are still diffed (the
persisted documents carry everything needed), but ``snapshots_comparable`` is
set to ``False`` and the response warns that candidate ranks/scores are not
directly comparable.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from . import db

# Classification vocabulary (also used by the API query parameter / CSV rows).
CATEGORIES = ("improved", "regressed", "score_only", "unchanged")

CATEGORY_LABELS = {
    "improved": "改善",
    "regressed": "退化",
    "score_only": "仅分数变化",
    "unchanged": "无变化",
}

# Status ordering: a higher rank is the "better" verdict.
STATUS_RANK = {"auto_matched": 2, "needs_review": 1, "no_match": 0}

STATUS_LABELS = {
    "auto_matched": "自动命中",
    "needs_review": "待确认",
    "no_match": "无匹配",
}

# Severity used when folding per-profile categories into one query badge:
# a regression on any profile dominates the row.
_CATEGORY_RANK = {"regressed": 3, "improved": 2, "score_only": 1, "unchanged": 0}


class RegressionValidationError(Exception):
    """Alignment failure (missing profiles/queries, duplicate queries, ...)."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("regression comparison validation failed")


# ---------------------------------------------------------------- validation --


def _err(kind: str, side: str | None, index: int | None, field: str | None,
         message: str) -> dict[str, Any]:
    return {"type": kind, "side": side, "index": index,
            "field": field, "message": message}


def _validate_compare_request(body: Any) -> tuple[str, str, list[dict[str, Any]]]:
    """Extract the two experiment ids from a raw request body."""
    errors: list[dict[str, Any]] = []
    if not isinstance(body, dict):
        return "", "", [_err("request", None, None, None, "请求体必须是 JSON 对象")]
    base = body.get("base_experiment_id")
    candidate = body.get("candidate_experiment_id")
    for field, value in (("base_experiment_id", base),
                         ("candidate_experiment_id", candidate)):
        if not isinstance(value, str) or not value.strip():
            errors.append(_err("request", None, None, field, f"{field} 不能为空"))
    if errors:
        return "", "", errors
    base_id, candidate_id = base.strip(), candidate.strip()
    if base_id == candidate_id:
        errors.append(_err(
            "request", None, None, "candidate_experiment_id",
            "基准实验与候选实验不能是同一个实验"))
    return base_id, candidate_id, errors


def _load_experiments(conn: sqlite3.Connection, base_id: str,
                      candidate_id: str) -> tuple[dict[str, Any],
                                                  dict[str, Any]]:
    from .experiments import serialize_experiment  # local import: avoid cycles

    errors: list[dict[str, Any]] = []
    base_row = db.get_ab_experiment(conn, base_id)
    cand_row = db.get_ab_experiment(conn, candidate_id)
    if base_row is None:
        errors.append(_err("experiment", "base", None, None,
                           f"基准实验 {base_id} 不存在"))
    if cand_row is None:
        errors.append(_err("experiment", "candidate", None, None,
                           f"候选实验 {candidate_id} 不存在"))
    if errors:
        raise RegressionValidationError(errors)
    return serialize_experiment(base_row), serialize_experiment(cand_row)


def _index_profiles(exp: dict[str, Any], side: str
                    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Map profiles by stripped name, flagging duplicate names within one run."""
    errors: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    for i, profile in enumerate(exp["profiles"]):
        name = str(profile.get("name", "")).strip()
        if name in by_name:
            errors.append(_err(
                "profile", side, i, "name",
                f"{'基准' if side == 'base' else '候选'}实验内档案名称重复：“{name}”"))
        else:
            by_name[name] = profile
    return by_name, errors


def _index_results(exp: dict[str, Any], side: str
                   ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Map per-query result documents by the stripped query text."""
    errors: list[dict[str, Any]] = []
    by_query: dict[str, dict[str, Any]] = {}
    for result in exp["results"]:
        query = str(result.get("query", "")).strip()
        if query in by_query:
            errors.append(_err(
                "query", side, result.get("index"), None,
                f"{'基准' if side == 'base' else '候选'}实验内查询重复：“{query}”"))
        else:
            by_query[query] = result
    return by_query, errors


# ---------------------------------------------------------------- comparison --


def _classify_cell(base_pr: dict[str, Any],
                   cand_pr: dict[str, Any]) -> tuple[str, list[str]]:
    """Classify one (base profile result, candidate profile result) pair."""
    notes: list[str] = []
    b_status, c_status = base_pr["status"], cand_pr["status"]
    b_rank, c_rank = STATUS_RANK[b_status], STATUS_RANK[c_status]
    b_best, c_best = base_pr["best_id"], cand_pr["best_id"]
    b_score, c_score = base_pr["best_score"], cand_pr["best_score"]

    if c_rank > b_rank:
        notes.append(
            f"状态 {STATUS_LABELS[b_status]} → {STATUS_LABELS[c_status]}")
        return "improved", notes
    if c_rank < b_rank:
        notes.append(
            f"状态 {STATUS_LABELS[b_status]} → {STATUS_LABELS[c_status]}")
        return "regressed", notes

    # Same status: a different *meaningful* preferred address is a regression.
    # For no_match the matcher still reports a low-scored best, so a different
    # best_id there is not a verdict change -- fall through to the score check.
    if b_best != c_best and b_status != "no_match":
        notes.append(
            f"状态保持 {STATUS_LABELS[b_status]}，首选地址 #{b_best} → #{c_best}")
        return "regressed", notes
    if b_best != c_best:
        notes.append(f"无匹配状态下候选首选 #{b_best} → #{c_best}（不影响结论）")

    if b_score != c_score:
        notes.append(
            f"首选得分 {_fmt_score(b_score)} → {_fmt_score(c_score)}")
        return "score_only", notes
    return "unchanged", notes


def _fmt_score(value: Any) -> str:
    return "—" if value is None else f"{value:.4f}"


def _address_text(per_query: dict[str, Any], profile_name: str,
                  address_id: Any) -> str | None:
    """Look up an address's full text inside a persisted per-query document."""
    if address_id is None:
        return None
    for pr in per_query["profiles"]:
        if pr["profile"] != profile_name:
            continue
        for cand in pr.get("candidates", []):
            if cand["address"]["id"] == address_id:
                return cand["address"]["full_text"]
    return None


def _compare_query(query: str, common_profiles: list[str],
                   base_exp: dict[str, Any], cand_exp: dict[str, Any],
                   base_qr: dict[str, Any], cand_qr: dict[str, Any]
                   ) -> dict[str, Any]:
    base_prs = {pr["profile"]: pr for pr in base_qr["profiles"]}
    cand_prs = {pr["profile"]: pr for pr in cand_qr["profiles"]}

    cells: list[dict[str, Any]] = []
    counts = {c: 0 for c in CATEGORIES}
    for name in common_profiles:
        b_pr, c_pr = base_prs[name], cand_prs[name]
        category, notes = _classify_cell(b_pr, c_pr)
        counts[category] += 1
        cells.append({
            "profile": name,
            "category": category,
            "category_label": CATEGORY_LABELS[category],
            "notes": notes,
            "base": {
                "status": b_pr["status"],
                "best_id": b_pr["best_id"],
                "best_text": _address_text(base_qr, name, b_pr["best_id"]),
                "best_score": b_pr["best_score"],
            },
            "candidate": {
                "status": c_pr["status"],
                "best_id": c_pr["best_id"],
                "best_text": _address_text(cand_qr, name, c_pr["best_id"]),
                "best_score": c_pr["best_score"],
            },
        })

    row_category = max(cells, key=lambda cell: _CATEGORY_RANK[cell["category"]])["category"]
    return {
        "query": query,
        "base_index": base_qr["index"],
        "candidate_index": cand_qr["index"],
        "category": row_category,
        "category_label": CATEGORY_LABELS[row_category],
        "counts": counts,
        "cells": cells,
    }


def build_comparison(conn: sqlite3.Connection, body: Any) -> dict[str, Any]:
    """Load two persisted experiments, align them and produce the comparison."""
    base_id, candidate_id, errors = _validate_compare_request(body)
    if errors:
        raise RegressionValidationError(errors)

    base_exp, cand_exp = _load_experiments(conn, base_id, candidate_id)

    # ---- align profiles by stripped name ----------------------------------
    base_profiles, e1 = _index_profiles(base_exp, "base")
    cand_profiles, e2 = _index_profiles(cand_exp, "candidate")
    errors = e1 + e2
    for name in base_profiles:
        if name not in cand_profiles:
            errors.append(_err(
                "profile", "candidate", None, "name",
                f"候选实验缺少档案“{name}”（基准实验中存在）"))
    for name in cand_profiles:
        if name not in base_profiles:
            errors.append(_err(
                "profile", "base", None, "name",
                f"基准实验缺少档案“{name}”（候选实验中存在）"))

    # ---- align queries by stripped text -----------------------------------
    base_queries, e3 = _index_results(base_exp, "base")
    cand_queries, e4 = _index_results(cand_exp, "candidate")
    errors += e3 + e4
    for i, query in enumerate(base_exp["queries"]):
        if query not in cand_queries:
            errors.append(_err(
                "query", "candidate", i, None,
                f"候选实验缺少查询“{query}”（基准实验第 {i + 1} 条）"))
    for i, query in enumerate(cand_exp["queries"]):
        if query not in base_queries:
            errors.append(_err(
                "query", "base", i, None,
                f"基准实验缺少查询“{query}”（候选实验第 {i + 1} 条）"))

    if errors:
        raise RegressionValidationError(errors)

    # Profile/query order follows the base experiment for stable output.
    common_profiles = [p["name"] for p in base_exp["profiles"]
                       if p["name"] in cand_profiles]
    common_queries = [q for q in base_exp["queries"] if q in cand_queries]

    rows = [
        _compare_query(query, common_profiles, base_exp, cand_exp,
                       base_queries[query], cand_queries[query])
        for query in common_queries
    ]

    totals = {c: 0 for c in CATEGORIES}
    for row in rows:
        for category, count in row["counts"].items():
            totals[category] += count

    base_snapshot = base_exp["table_snapshot"]
    cand_snapshot = cand_exp["table_snapshot"]
    snapshots_comparable = base_snapshot.get("digest") == cand_snapshot.get("digest")

    return {
        "base": {
            "id": base_exp["id"],
            "created_at": base_exp["created_at"],
            "address_count": base_exp["address_count"],
            "table_snapshot": base_snapshot,
        },
        "candidate": {
            "id": cand_exp["id"],
            "created_at": cand_exp["created_at"],
            "address_count": cand_exp["address_count"],
            "table_snapshot": cand_snapshot,
        },
        "profiles": common_profiles,
        "query_count": len(rows),
        "snapshots_comparable": snapshots_comparable,
        "warning": (
            None if snapshots_comparable else
            "两次实验的标准地址表快照 digest 不同（基准 "
            f"{base_snapshot.get('count')} 条 / 候选 "
            f"{cand_snapshot.get('count')} 条），候选排名与得分不可直接比较；"
            "以下逐条差异仍然展示，仅供人工参考。"
        ),
        "summary": {
            "by_cell": {c: {"label": CATEGORY_LABELS[c], "count": totals[c]}
                        for c in CATEGORIES},
            "by_query": {
                c: {"label": CATEGORY_LABELS[c],
                    "count": sum(1 for r in rows if r["category"] == c)}
                for c in CATEGORIES
            },
        },
        "rows": rows,
    }


# -------------------------------------------------------------------- CSV I/O --


def comparison_csv(report: dict[str, Any],
                   category: str | None = None) -> str:
    """Serialize the comparison report as UTF-8 CSV text.

    One row per (query × profile); ``category`` restricts the rows to a single
    per-cell category (the web UI uses it to match the on-screen filter).
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "查询", "档案", "分类",
        "基准-状态", "候选-状态",
        "基准-首选ID", "基准-首选地址", "基准-首选得分",
        "候选-首选ID", "候选-首选地址", "候选-首选得分",
        "快照可直接比较", "说明",
    ])
    comparable = "是" if report["snapshots_comparable"] else "否"
    for row in report["rows"]:
        for cell in row["cells"]:
            if category and cell["category"] != category:
                continue
            b, c = cell["base"], cell["candidate"]
            writer.writerow([
                row["query"], cell["profile"],
                CATEGORY_LABELS[cell["category"]],
                STATUS_LABELS[b["status"]], STATUS_LABELS[c["status"]],
                b["best_id"] if b["best_id"] is not None else "",
                b["best_text"] or "", b["best_score"] if b["best_score"] is not None else "",
                c["best_id"] if c["best_id"] is not None else "",
                c["best_text"] or "", c["best_score"] if c["best_score"] is not None else "",
                comparable, "；".join(cell["notes"]),
            ])
    return buffer.getvalue()
