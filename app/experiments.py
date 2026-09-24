"""规则 A/B 对照实验 (rule A/B comparison experiments).

The user submits 2~4 named *rule profiles* (auto/reject/margin thresholds and
the token-weight parameter) together with 1~100 queries.  Every query is run
through the **existing matcher** once per profile, against the same frozen
snapshot of the standard-address table (the rows are read once and reused).

The experiment is a pure scoring comparison: the review-consensus memory is
not consulted and nothing is written to ``match_logs``.  Invalid profile
parameters or duplicated queries are reported item-by-item and, when any such
error exists, the experiment is not executed at all.

Results are persisted in ``ab_experiments`` and can be replayed by experiment
id.  Matching itself is deterministic, so the same input order always yields
the same result document.
"""
from __future__ import annotations

import dataclasses
import json
import math
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import Settings
from .matcher import match_address, result_to_dict

# Settings fields that a rule profile is allowed to override.
_PROFILE_FIELDS = ("auto_threshold", "reject_threshold",
                   "margin_threshold", "token_weight")

# 2~4 profiles, 1~100 queries (also enforced by the OpenAPI schema).
MIN_PROFILES, MAX_PROFILES = 2, 4
MIN_QUERIES, MAX_QUERIES = 1, 100


class ExperimentValidationError(Exception):
    """Semantic validation failure (e.g. duplicate names/queries)."""

    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("experiment validation failed")


# ---------------------------------------------------------------- validation --


def _err(kind: str, index: int | None, field: str | None,
         message: str) -> dict[str, Any]:
    return {"type": kind, "index": index, "field": field, "message": message}


def _as_number(value: Any) -> float | None:
    """Numbers only; booleans and numeric strings are rejected on purpose."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def validate_payload(body: Any) -> tuple[list[dict[str, Any]], list[str],
                                          list[dict[str, Any]]]:
    """Validate a raw experiment request body item by item.

    Every invalid profile/query is reported independently -- the request is
    never partially executed.  Returns ``(profiles, queries, errors)``; when
    ``errors`` is non-empty the caller must abort without running anything.
    """
    errors: list[dict[str, Any]] = []

    if not isinstance(body, dict):
        return [], [], [_err("request", None, None, "请求体必须是 JSON 对象")]

    raw_profiles = body.get("profiles")
    raw_queries = body.get("queries")
    profiles_ok = isinstance(raw_profiles, list)
    queries_ok = isinstance(raw_queries, list)
    # A missing field counts as a cardinality error (both fields are
    # required); a present field of the wrong type gets a type error and the
    # cardinality check is then skipped for it.
    if not profiles_ok and raw_profiles is not None:
        errors.append(_err("profiles", None, None,
                           "profiles 必须是 2~4 个规则档案组成的数组"))
    if not queries_ok and raw_queries is not None:
        errors.append(_err("queries", None, None,
                           "queries 必须是 1~100 条查询组成的数组"))
    if not profiles_ok:
        raw_profiles = []
    if not queries_ok:
        raw_queries = []

    # Cardinality is checked for list-shaped or missing inputs, never twice
    # for a field already reported as the wrong type.
    check_profile_count = profiles_ok or "profiles" not in body
    check_query_count = queries_ok or "queries" not in body
    if check_profile_count and not (
        MIN_PROFILES <= len(raw_profiles) <= MAX_PROFILES
    ):
        errors.append(_err("profiles", None, None,
                           f"规则档案数量必须为 {MIN_PROFILES}~{MAX_PROFILES} 个，"
                           f"当前 {len(raw_profiles)} 个"))
    if check_query_count and not (
        MIN_QUERIES <= len(raw_queries) <= MAX_QUERIES
    ):
        errors.append(_err("queries", None, None,
                           f"查询数量必须为 {MIN_QUERIES}~{MAX_QUERIES} 条，"
                           f"当前 {len(raw_queries)} 条"))

    profiles: list[dict[str, Any]] = []
    seen_names: dict[str, int] = {}
    for i, item in enumerate(raw_profiles):
        if not isinstance(item, dict):
            errors.append(_err("profile", i, None, "档案必须是 JSON 对象"))
            continue
        profile: dict[str, Any] = {}
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(_err("profile", i, "name", "档案名称不能为空"))
        elif len(name.strip()) > 40:
            errors.append(_err("profile", i, "name", "档案名称不能超过 40 个字符"))
        else:
            profile["name"] = name.strip()
            if profile["name"] in seen_names:
                errors.append(_err(
                    "profile", i, "name",
                    f"档案名称与第 {seen_names[profile['name']] + 1} 档重复："
                    f"“{profile['name']}”"))
            else:
                seen_names[profile["name"]] = i

        for field in _PROFILE_FIELDS:
            number = _as_number(item.get(field))
            if number is None:
                errors.append(_err(
                    "profile", i, field,
                    f"{field} 必须是 0~1 之间的有限数值"))
                continue
            if not 0.0 <= number <= 1.0:
                errors.append(_err(
                    "profile", i, field,
                    f"{field}={number:g} 超出允许范围 [0, 1]"))
                continue
            profile[field] = number

        if {"reject_threshold", "auto_threshold"} <= profile.keys():
            if profile["reject_threshold"] >= profile["auto_threshold"]:
                errors.append(_err(
                    "profile", i, "reject_threshold",
                    f"拒绝阈值（{profile['reject_threshold']:g}）必须严格小于自动命中阈值"
                    f"（{profile['auto_threshold']:g}）"))
        profiles.append(profile)

    queries: list[str] = []
    seen_queries: dict[str, int] = {}
    for i, item in enumerate(raw_queries):
        if not isinstance(item, str) or not item.strip():
            errors.append(_err("query", i, None, "查询不能为空且必须是字符串"))
            continue
        query = item.strip()
        # Duplicates are detected on the stripped strings (case sensitive):
        # "Main St" and "main st" are different spellings worth comparing,
        # byte-identical submissions are not.
        if query in seen_queries:
            errors.append(_err(
                "query", i, None,
                f"查询与第 {seen_queries[query] + 1} 条完全重复：“{query}”"))
        else:
            seen_queries[query] = i
        queries.append(query)

    return profiles, queries, errors


# ------------------------------------------------------------------- running --


def _threshold_block(result: Any, settings: Settings) -> dict[str, Any]:
    """Threshold values, measured quantities and which condition fired.

    The decision order mirrors ``matcher.match_address`` exactly: rejection is
    tested first, then the top-two margin, then the auto cut-off.  ``decisive``
    names the threshold that actually determined the verdict (``None`` for an
    automatic hit).
    """
    best = result.best
    runner = result.runner_up
    best_score = best.score if best else None
    gap = round(best.score - runner.score, 4) if (best and runner) else None

    reject_fired = best is None or best.score < settings.reject_threshold
    margin_fired = runner is not None and gap is not None and gap < settings.margin_threshold
    auto_fired = best is not None and best.score < settings.auto_threshold

    if reject_fired:
        decisive = "reject_threshold"
    elif margin_fired:
        decisive = "margin_threshold"
    elif auto_fired:
        decisive = "auto_threshold"
    else:
        decisive = None

    return {
        "auto_threshold": {
            "value": settings.auto_threshold,
            "actual": best_score,
            "triggered": bool(auto_fired),
        },
        "reject_threshold": {
            "value": settings.reject_threshold,
            "actual": best_score,
            "triggered": bool(reject_fired),
        },
        "margin_threshold": {
            "value": settings.margin_threshold,
            "actual": gap,
            "triggered": bool(margin_fired),
        },
        "token_weight": {"value": settings.token_weight},
        "decisive": decisive,
    }


def _candidate_diff(profile_names: list[str],
                    per_profile: list[dict[str, Any]]) -> dict[str, Any]:
    """Union of every profile's candidate list with per-profile rank/score.

    Union order follows the first appearance of an address (profiles in
    submitted order, candidates in score order), which is fully deterministic.
    """
    union_ids: list[int] = []
    addresses: dict[int, dict[str, Any]] = {}
    cells: dict[int, dict[str, dict[str, Any]]] = {}

    for pr in per_profile:
        for rank, cand in enumerate(pr["candidates"]):
            aid = cand["address"]["id"]
            if aid not in cells:
                cells[aid] = {}
                union_ids.append(aid)
                addresses[aid] = {
                    "id": aid,
                    "full_text": cand["address"]["full_text"],
                    "house": cand["address"]["house"],
                }
            cells[aid][pr["profile"]] = {"rank": rank + 1, "score": cand["score"]}

    ranking = []
    for aid in union_ids:
        row = {
            "address_id": aid,
            "full_text": addresses[aid]["full_text"],
            "house": addresses[aid]["house"],
            "cells": [
                {"profile": name, "match": cells[aid].get(name)}
                for name in profile_names
            ],
        }
        ranking.append(row)
    return {"ranking": ranking}


def _run_one_query(query: str, rows: list[sqlite3.Row],
                   profiles: list[dict[str, Any]], base_settings: Settings,
                   counters: list[dict[str, int]], query_index: int
                   ) -> dict[str, Any]:
    profile_names = [p["name"] for p in profiles]
    per_profile: list[dict[str, Any]] = []
    normalized: dict[str, Any] | None = None
    first_best_score: float | None = None

    for pi, profile in enumerate(profiles):
        settings = dataclasses.replace(
            base_settings,
            **{field: profile[field] for field in _PROFILE_FIELDS},
        )
        result = match_address(query, rows, settings)
        data = result_to_dict(result)
        data.pop("consensus", None)  # experiments compare raw scoring only
        normalized = {
            "text": result.normalized_query.text,
            "tokens": result.normalized_query.tokens,
            "house": result.normalized_query.house,
        }

        best_score = data["best_score"]
        if pi == 0:
            first_best_score = best_score
        score_delta = (
            round(best_score - first_best_score, 4)
            if best_score is not None and first_best_score is not None
            else None
        )

        counters[pi][result.status] += 1
        per_profile.append({
            "profile": profile["name"],
            "index": pi,
            "status": data["status"],
            "reason": data["reason"],
            "best_id": data["best_id"],
            "best_score": best_score,
            "runner_up_id": data["runner_up_id"],
            "runner_up_score": data["runner_up_score"],
            "score_delta_vs_first": score_delta,
            "normalized": normalized,
            "thresholds": _threshold_block(result, settings),
            "candidates": data["candidates"],
        })

    # Cross-profile conclusion comparison.
    status_groups: dict[str, list[str]] = {}
    for pr in per_profile:
        status_groups.setdefault(pr["status"], []).append(pr["profile"])
    statuses_agree = len(status_groups) == 1
    best_ids = {pr["best_id"] for pr in per_profile}
    best_id_agree = len(best_ids) == 1
    disagreement = not (statuses_agree and best_id_agree)

    best_scores = [pr["best_score"] for pr in per_profile
                   if pr["best_score"] is not None]
    score_range = {
        "min": min(best_scores) if best_scores else None,
        "max": max(best_scores) if best_scores else None,
        "delta": round(max(best_scores) - min(best_scores), 4)
        if best_scores else None,
    }

    return {
        "index": query_index,
        "query": query,
        "disagreement": {
            "status_agree": statuses_agree,
            "best_id_agree": best_id_agree,
            "conclusion_differs": disagreement,
            "status_groups": status_groups,
            "best_score_range": score_range,
        },
        "profiles": per_profile,
        "candidate_diff": _candidate_diff(profile_names, per_profile),
    }


def run_experiment(conn: sqlite3.Connection, body: Any,
                   base_settings: Settings) -> dict[str, Any]:
    """Validate, run every profile on every query and persist the experiment.

    Raises :class:`ExperimentValidationError` (with item-by-item errors) when
    any profile parameter or query is invalid; in that case nothing runs and
    nothing is persisted.
    """
    profiles, queries, errors = validate_payload(body)
    if errors:
        raise ExperimentValidationError(errors)

    # One snapshot of the address table for the whole experiment.  The rows
    # and the digest are read inside a single BEGIN IMMEDIATE transaction, so
    # no concurrent upload can mutate the table between scoring and recording
    # the snapshot the scores were computed against.
    counters = [
        {"name": p["name"], "auto_matched": 0, "needs_review": 0, "no_match": 0}
        for p in profiles
    ]

    results: list[dict[str, Any]] = []
    with db.immediate_transaction(conn):
        rows = db.all_addresses(conn)
        for qi, query in enumerate(queries):
            results.append(
                _run_one_query(query, rows, profiles, base_settings, counters, qi)
            )
        snapshot_json = db.address_table_snapshot(conn)
        snapshot = json.loads(snapshot_json)

        now = datetime.now(timezone.utc)
        experiment_id = f"EXP-{now.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}"
        created_at = now.isoformat(timespec="seconds")
        summary = {
            "profiles": counters,
            "query_count": len(queries),
            "disagreement_count": sum(
                1 for r in results if r["disagreement"]["conclusion_differs"]
            ),
        }
        db.insert_ab_experiment(
            conn,
            experiment_id=experiment_id,
            profiles=profiles,
            queries=queries,
            table_snapshot=snapshot_json,
            address_count=len(rows),
            results=results,
            summary=summary,
            created_at=created_at,
        )

    return {
        "id": experiment_id,
        "created_at": created_at,
        "address_count": len(rows),
        "table_snapshot": snapshot,
        "profiles": profiles,
        "queries": queries,
        "summary": summary,
        "results": results,
    }


# ------------------------------------------------------------- serialization --


def serialize_experiment(row: sqlite3.Row) -> dict[str, Any]:
    """Reconstruct the full result document from a persisted row."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "address_count": row["address_count"],
        "table_snapshot": json.loads(row["table_snapshot"]),
        "profiles": json.loads(row["profiles_json"]),
        "queries": json.loads(row["queries_json"]),
        "summary": json.loads(row["summary_json"]),
        "results": json.loads(row["results_json"]),
    }


def serialize_experiment_summary(row: sqlite3.Row) -> dict[str, Any]:
    """Lightweight shape for the experiment history list."""
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "address_count": row["address_count"],
        "profiles": json.loads(row["profiles_json"]),
        "query_count": len(json.loads(row["queries_json"])),
        "summary": json.loads(row["summary_json"]),
    }
