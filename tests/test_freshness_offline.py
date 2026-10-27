"""scripts/check_network_freshness.py: stale and thinning networks, offline."""

from __future__ import annotations

import json
from datetime import date, timedelta

from scripts import check_network_freshness as fresh

TODAY = date(2026, 10, 27)


def _feature(client, code, daily=True, status="listed"):
    return {
        "properties": {
            "client": client,
            "code": code,
            "daily_or_better": daily,
            "upstream_status": status,
        }
    }


def _csv(directory, code, last, days=20, value="1.0"):
    rows = ["date,wteq_cm,snwd_cm"]
    for i in range(days - 1, -1, -1):
        rows.append(f"{last - timedelta(days=i)},{value},")
    (directory / f"{code}.csv").write_text("\n".join(rows) + "\n")


def test_a_network_with_no_new_rows_is_stale(tmp_path):
    # Yukon on 2026-10-27: newest row 2026-10-14.
    _csv(tmp_path, "09AA-M1", date(2026, 10, 14))
    _csv(tmp_path, "05AA809_AB_MSNT", TODAY - timedelta(days=1))
    nets = fresh.assess(
        [_feature("yukon", "09AA-M1"), _feature("alberta", "05AA809_AB_MSNT")],
        tmp_path,
        TODAY,
    )
    assert nets["yukon"].reasons == ["no new data for 13 days (newest row 2026-10-14)"]
    assert nets["alberta"].reasons == []


def test_seven_days_old_is_not_yet_stale(tmp_path):
    _csv(tmp_path, "A", TODAY - timedelta(days=7))
    nets = fresh.assess([_feature("awdb", "A")], tmp_path, TODAY)
    assert nets["awdb"].reasons == []


def test_a_network_losing_most_stations_is_flagged(tmp_path):
    feats = []
    for i in range(6):
        code = f"S{i}"
        feats.append(_feature("cdec", code))
        # Two keep reporting; four stopped nine days ago.
        _csv(tmp_path, code, TODAY - timedelta(days=1 if i < 2 else 9))
    nets = fresh.assess(feats, tmp_path, TODAY)
    assert nets["cdec"].recent == 2 and nets["cdec"].prior == 6
    assert nets["cdec"].reasons == [
        "2 of 6 daily stations reported in the last 7 days, down from 6 the week before"
    ]


def test_missing_non_daily_and_never_fetched_stations_are_ignored(tmp_path):
    _csv(tmp_path, "OLD", date(2020, 1, 1))
    feats = [
        _feature("nve", "OLD", status="missing"),
        _feature("nve", "SC", daily=False),
        _feature("ceaza", "NOCSV"),
    ]
    _csv(tmp_path, "SC", date(2020, 1, 1))
    nets = fresh.assess(feats, tmp_path, TODAY)
    assert "nve" not in nets  # both excluded before counting
    assert nets["ceaza"].newest is None and nets["ceaza"].reasons == []


def test_empty_value_rows_do_not_count_as_data(tmp_path):
    _csv(tmp_path, "E", TODAY, value="")
    _csv(tmp_path, "F", date(2026, 10, 1))
    path = tmp_path / "F.csv"
    path.write_text(path.read_text() + f"{TODAY},,\n")
    nets = fresh.assess([_feature("slf", "E"), _feature("slf", "F")], tmp_path, TODAY)
    assert nets["slf"].newest == date(2026, 10, 1)


def test_rows_after_today_are_ignored(tmp_path):
    _csv(tmp_path, "A", TODAY + timedelta(days=5))
    nets = fresh.assess([_feature("awdb", "A")], tmp_path, TODAY)
    assert nets["awdb"].newest == TODAY


class FakeGitHub:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    def ensure_label(self):
        pass

    def open_issues(self):
        return self.issues

    def create(self, title, body):
        self.calls.append(("create", title))
        return {"number": 9}

    def update(self, number, **fields):
        self.calls.append(("update", number, fields.get("state")))
        return {}

    def comment(self, number, body):
        self.calls.append(("comment", number))
        return {}


def test_report_opens_for_stale_and_closes_on_recovery():
    stale = fresh.Network("yukon", stations=17, newest=date(2026, 10, 14))
    stale.reasons = ["no new data for 13 days (newest row 2026-10-14)"]
    ok = fresh.Network("awdb", stations=10, newest=TODAY, recent=10, prior=10)
    github = FakeGitHub(
        [{"number": 3, "title": "x", "body": "<!-- gsn-freshness: awdb -->"}]
    )
    fresh.report({"yukon": stale, "awdb": ok}, github, TODAY, log=lambda _: None)
    assert (
        "create",
        "No fresh data from yukon: no new data for 13 days (newest row 2026-10-14)",
    ) in github.calls
    assert ("comment", 3) in github.calls
    assert ("update", 3, "closed") in github.calls


def test_main_dry_run_prints_an_annotation(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _csv(tmp_path, "09AA-M1", date(2026, 10, 14))
    inv = tmp_path / "inv.geojson"
    inv.write_text(json.dumps({"features": [_feature("yukon", "09AA-M1")]}))
    args = ["--today", "2026-10-27", "--inventory", str(inv)]
    args += ["--stations-dir", str(tmp_path), "--dry-run"]
    assert fresh.main(args) == 0
    out = capsys.readouterr().out
    assert "::error title=Stale network yukon::no new data for 13 days" in out
