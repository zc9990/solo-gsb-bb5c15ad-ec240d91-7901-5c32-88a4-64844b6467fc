"""Tests for the historical regression comparison between two experiments."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import ingest  # noqa: E402
from app.main import app, get_conn, get_settings  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolated_tables(client):
    conn = get_conn()
    conn.execute("DELETE FROM ab_experiments")
    conn.execute("DELETE FROM match_logs")
    conn.execute("DELETE FROM addresses")
    conn.commit()
    sample = Path(get_settings().sample_csv)
    ingest.add_many(conn, ingest.parse_csv(sample.read_bytes()))
    yield
    conn.execute("DELETE FROM ab_experiments")
    conn.commit()


def _profile(name="A", **overrides):
    base = {
        "name": name,
        "auto_threshold": 0.8,
        "reject_threshold": 0.35,
        "margin_threshold": 0.06,
        "token_weight": 0.7,
    }
    base.update(overrides)
    return base


def _run(client, *, profiles=None, queries):
    body = {
        "profiles": profiles or [_profile("基线A"), _profile("激进B", auto_threshold=0.7)],
        "queries": queries,
    }
    r = client.post("/api/experiments", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _compare(client, base_id, candidate_id, expected=200):
    r = client.post("/api/experiments/compare", json={
        "base_experiment_id": base_id,
        "candidate_experiment_id": candidate_id,
    })
    assert r.status_code == expected, r.text
    return r.json()


# ---------------------------------------------------------------- happy path


def test_compare_identical_experiments_all_unchanged(client):
    queries = ["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas",
               "1 Milky Way, Mars Base"]
    first = _run(client, queries=queries)
    second = _run(client, queries=queries)
    report = _compare(client, first["id"], second["id"])

    assert report["base"]["id"] == first["id"]
    assert report["candidate"]["id"] == second["id"]
    assert report["profiles"] == ["基线A", "激进B"]
    assert report["query_count"] == 3
    assert report["snapshots_comparable"] is True
    assert report["warning"] is None
    # every (query x profile) cell and every query row is unchanged
    assert report["summary"]["by_cell"]["unchanged"]["count"] == 6
    assert report["summary"]["by_query"]["unchanged"]["count"] == 3
    for category in ("improved", "regressed", "score_only"):
        assert report["summary"]["by_cell"][category]["count"] == 0
    assert all(row["category"] == "unchanged" for row in report["rows"])


def test_compare_detects_improvement_and_regression(client):
    # Strict profile: the Lake Dr query (score ~0.79) stays needs_review and
    # the junk query is rejected; the relaxed profile auto-matches Lake Dr.
    strict = [_profile("基线A"), _profile("激进B", auto_threshold=0.7)]
    relaxed = [_profile("基线A", auto_threshold=0.7),
               _profile("激进B", auto_threshold=0.65, reject_threshold=0.1)]
    queries = ["1010 Lake Dr, Austin TX", "1 Milky Way, Mars Base"]

    old = _run(client, profiles=strict, queries=queries)
    new = _run(client, profiles=relaxed, queries=queries)
    report = _compare(client, old["id"], new["id"])

    lake = next(r for r in report["rows"] if r["query"] == queries[0])
    junk = next(r for r in report["rows"] if r["query"] == queries[1])

    # 基线A: needs_review -> auto_matched on Lake Dr = improvement
    base_cell = next(c for c in lake["cells"] if c["profile"] == "基线A")
    assert base_cell["category"] == "improved"
    assert base_cell["base"]["status"] == "needs_review"
    assert base_cell["candidate"]["status"] == "auto_matched"

    # junk query scores 0.0: it stays no_match even with reject=0.1, so the
    # cell is unchanged (best_id differences under no_match never regress).
    junk_b = next(c for c in junk["cells"] if c["profile"] == "激进B")
    assert junk_b["base"]["status"] == "no_match"
    assert junk_b["candidate"]["status"] == "no_match"
    assert junk_b["category"] == "unchanged"

    # tallies are internally consistent
    for category in ("improved", "regressed", "score_only", "unchanged"):
        assert report["summary"]["by_cell"][category]["count"] == sum(
            row["counts"][category] for row in report["rows"])

    # swapping base/candidate turns the improvement into a regression
    swapped = _compare(client, new["id"], old["id"])
    lake_s = next(r for r in swapped["rows"] if r["query"] == queries[0])
    assert next(c for c in lake_s["cells"] if c["profile"] == "基线A")["category"] == "regressed"


def test_compare_score_only_change(client):
    # Same thresholds/status/best address, different token weight -> only the
    # score moves (pick a query that auto-matches under both weights).
    p1 = [_profile("同档", token_weight=0.7),
          _profile("次档", token_weight=0.8)]
    p2 = [_profile("同档", token_weight=0.9),
          _profile("次档", token_weight=0.8)]
    old = _run(client, profiles=p1, queries=["123 Main Street, New York, NY 10001"])
    new = _run(client, profiles=p2, queries=["123 Main Street, New York, NY 10001"])
    report = _compare(client, old["id"], new["id"])
    cell = report["rows"][0]["cells"][0]
    assert cell["profile"] == "同档"
    assert cell["category"] in {"score_only", "unchanged"}
    assert cell["base"]["best_id"] == cell["candidate"]["best_id"]


def test_row_category_dominated_by_regression(client):
    # One profile improves while another regresses on the same query -> the
    # query-level badge must be the worst category (regressed).
    old_profiles = [
        _profile("P1", auto_threshold=0.8),   # Lake Dr -> needs_review
        _profile("P2", auto_threshold=0.7),   # Lake Dr -> auto_matched
    ]
    new_profiles = [
        _profile("P1", auto_threshold=0.7),   # -> auto_matched (improved)
        _profile("P2", auto_threshold=0.8),   # -> needs_review (regressed)
    ]
    old = _run(client, profiles=old_profiles, queries=["1010 Lake Dr, Austin TX"])
    new = _run(client, profiles=new_profiles, queries=["1010 Lake Dr, Austin TX"])
    report = _compare(client, old["id"], new["id"])
    row = report["rows"][0]
    assert row["category"] == "regressed"
    assert {c["category"] for c in row["cells"]} == {"improved", "regressed"}


# ----------------------------------------------------------- alignment errors


def test_compare_missing_experiment_returns_404(client):
    one = _run(client, queries=["1010 Lake Dr, Austin TX"])
    data = _compare(client, one["id"], "EXP-does-not-exist", expected=404)
    assert data["errors"]
    assert all(e["type"] == "experiment" for e in data["errors"])
    assert any("候选" in e["message"] for e in data["errors"])

    data = _compare(client, "EXP-missing-a", "EXP-missing-b", expected=404)
    assert len(data["errors"]) == 2


def test_compare_same_experiment_rejected(client):
    one = _run(client, queries=["1010 Lake Dr, Austin TX"])
    data = _compare(client, one["id"], one["id"], expected=400)
    assert any("同一个实验" in e["message"] for e in data["errors"])


def test_compare_rejects_missing_profile(client):
    old = _run(client, profiles=[_profile("A"), _profile("B")],
               queries=["1010 Lake Dr, Austin TX"])
    new = _run(client, profiles=[_profile("A"), _profile("C")],
               queries=["1010 Lake Dr, Austin TX"])
    data = _compare(client, old["id"], new["id"], expected=400)
    messages = [e["message"] for e in data["errors"]]
    assert any(e["type"] == "profile" and "候选实验缺少档案“B”" in e["message"] for e in data["errors"])
    assert any("基准实验缺少档案“C”" in m for m in messages)


def test_compare_rejects_missing_and_extra_queries(client):
    old = _run(client, queries=["1010 Lake Dr, Austin TX", "Hillcrest Road, Dallas"])
    new = _run(client, queries=["1010 Lake Dr, Austin TX", "1 Milky Way, Mars Base"])
    data = _compare(client, old["id"], new["id"], expected=400)
    assert any(e["type"] == "query" and "候选实验缺少查询" in e["message"]
               and "Hillcrest" in e["message"] for e in data["errors"])
    assert any(e["type"] == "query" and "基准实验缺少查询" in e["message"]
               and "Milky Way" in e["message"] for e in data["errors"])


def test_compare_aligns_on_stripped_query_text(client):
    # Whitespace-only differences in query and profile presentation still align.
    old = _run(client,
               profiles=[{"name": " 基线A ", "auto_threshold": 0.8,
                          "reject_threshold": 0.35, "margin_threshold": 0.06,
                          "token_weight": 0.7},
                         _profile("B")],
               queries=["1010 Lake Dr, Austin TX"])
    new = _run(client,
               profiles=[_profile("基线A"), _profile("B")],
               queries=["  1010 Lake Dr, Austin TX  "])
    report = _compare(client, old["id"], new["id"])
    assert report["profiles"] == ["基线A", "B"]
    assert report["query_count"] == 1
    assert report["rows"][0]["query"] == "1010 Lake Dr, Austin TX"


def test_compare_empty_body_rejected(client):
    r = client.post("/api/experiments/compare", json={})
    assert r.status_code == 400
    fields = {e["field"] for e in r.json()["errors"]}
    assert "base_experiment_id" in fields
    assert "candidate_experiment_id" in fields


# ----------------------------------------------------- different snapshots


def test_compare_different_snapshot_digest_warns_but_diff_shown(client):
    queries = ["304 Lake Dr, Austin TX"]
    old = _run(client, queries=queries)

    client.post("/api/addresses", json={"addresses": [
        {"address": "305 Lake Drive, Austin, TX 73301", "external_id": "RG-305"},
    ]})
    new = _run(client, queries=queries)
    assert old["table_snapshot"]["digest"] != new["table_snapshot"]["digest"]

    report = _compare(client, old["id"], new["id"])
    assert report["snapshots_comparable"] is False
    assert "digest 不同" in report["warning"]
    # per-row diffs are still produced from the persisted documents
    assert len(report["rows"]) == 1
    assert report["rows"][0]["cells"]


# ----------------------------------------------------------------- CSV / API


def test_comparison_csv_download(client):
    queries = ["1010 Lake Dr, Austin TX"]
    old = _run(client,
               profiles=[_profile("基线A"), _profile("激进B", auto_threshold=0.7)],
               queries=queries)
    new = _run(client,
               profiles=[_profile("基线A", auto_threshold=0.7),
                         _profile("激进B", auto_threshold=0.7)],
               queries=queries)

    r = client.get("/api/experiments/compare/csv", params={
        "base_experiment_id": old["id"],
        "candidate_experiment_id": new["id"],
    })
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert old["id"] in r.headers["content-disposition"]
    text = r.text.lstrip("﻿")
    assert "查询" in text and "档案" in text and "分类" in text
    # one data row per aligned profile (2 profiles x 1 query)
    assert len([line for line in text.splitlines() if line]) == 3
    assert "改善" in text  # 基线A needs_review -> auto_matched

    # category filter keeps only matching cells
    r2 = client.get("/api/experiments/compare/csv", params={
        "base_experiment_id": old["id"],
        "candidate_experiment_id": new["id"],
        "category": "improved",
    })
    body = r2.text.lstrip("﻿")
    assert len([line for line in body.splitlines() if line]) == 2  # header + 1

    r3 = client.get("/api/experiments/compare/csv", params={
        "base_experiment_id": old["id"],
        "candidate_experiment_id": new["id"],
        "category": "bogus",
    })
    assert r3.status_code == 400
