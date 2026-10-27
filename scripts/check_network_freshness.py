"""Flag a network whose daily data stopped arriving, and say so on GitHub.

From 2026-10-15 to 2026-10-27 the Yukon AquaCache API returned no
continuous series to the client (an upstream column rename), so not one of
its 17 daily stations got a new row. Every stage of the daily workflow
stayed green: the station builder logged the loss and carried the stations
forward, the fetch had nothing to fetch, and finalize committed what it had.
The archive alone shows the gap, so this script reads the archive.

For each network (``client``), over the stations it lists as reporting
daily (``daily_or_better``) and not marked missing upstream:

* **stale**: the newest row in any of its CSVs is more than ``--stale-days``
  (7) days old;
* **dropping**: fewer than half as many of its stations reported in the last
  7 days as in the 7 days before (and at least 4 did then), which catches a
  network losing part of its stations rather than all of them.

Everything is computed from ``all_snow_stations.geojson`` and
``data/stations/*.csv``, so it needs no state of its own. It prints a table,
appends it to the Actions job summary, emits one ``::error`` annotation per
flagged network, and, when ``GITHUB_TOKEN`` is set, opens one issue per
flagged network (labelled ``network-health``), updates it on later runs,
and closes it when the network recovers. It always exits 0: a stale network
must not block the map deploy.

Usage
-----
    python -m scripts.check_network_freshness [--today YYYY-MM-DD]
        [--stale-days 7] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "all_snow_stations.geojson"
STATIONS_DIR = ROOT / "data" / "stations"

LABEL = "network-health"
LABEL_COLOR = "d93f0b"
LABEL_DESCRIPTION = "A network's daily data stopped or thinned out"
MARKER = "<!-- gsn-freshness: {client} -->"

WINDOW_DAYS = 7
DROP_FRACTION = 0.5
DROP_MIN_PRIOR = 4
#: Rows read from the end of each CSV: enough to cover two windows of daily
#: data, and hourly-resampled networks are already one row per day.
TAIL_ROWS = 40


@dataclass
class Network:
    client: str
    stations: int = 0
    newest: date | None = None
    recent: int = 0  # stations with a value in the last window
    prior: int = 0  # stations with a value in the window before
    reasons: list[str] = field(default_factory=list)


def recent_dates(path: Path, tail: int = TAIL_ROWS) -> list[date]:
    """Dates of the last *tail* rows of a station CSV that carry a value."""
    try:
        with path.open(encoding="utf-8") as fh:
            header = fh.readline()
            rows = deque(fh, maxlen=tail)
    except OSError:
        return []
    if not header.startswith("date"):
        return []
    out = []
    for row in rows:
        cells = row.rstrip("\n").split(",")
        if len(cells) > 1 and any(c.strip() for c in cells[1:]):
            try:
                out.append(date.fromisoformat(cells[0][:10]))
            except ValueError:
                continue
    return out


def assess(
    features: list[dict],
    stations_dir: Path,
    today: date,
    *,
    stale_days: int = WINDOW_DAYS,
) -> dict[str, Network]:
    """Freshness of every network's daily stations as of *today*."""
    recent_start = today - timedelta(days=WINDOW_DAYS)
    prior_start = recent_start - timedelta(days=WINDOW_DAYS)
    networks: dict[str, Network] = {}
    for feature in features:
        p = feature.get("properties", {})
        if not p.get("daily_or_better") or p.get("upstream_status") == "missing":
            continue
        client, code = p.get("client"), p.get("code")
        if not client or not code:
            continue
        net = networks.setdefault(client, Network(client))
        net.stations += 1
        dates = [d for d in recent_dates(stations_dir / f"{code}.csv") if d <= today]
        if not dates:
            continue
        last = max(dates)
        if net.newest is None or last > net.newest:
            net.newest = last
        if any(d >= recent_start for d in dates):
            net.recent += 1
        if any(prior_start <= d < recent_start for d in dates):
            net.prior += 1

    for net in networks.values():
        if net.newest is None:
            # Never fetched (e.g. a network gated on a secret not yet set):
            # nothing to have stopped.
            continue
        age = (today - net.newest).days
        if age > stale_days:
            net.reasons.append(f"no new data for {age} days (newest row {net.newest})")
        elif net.prior >= DROP_MIN_PRIOR and net.recent < DROP_FRACTION * net.prior:
            net.reasons.append(
                f"{net.recent} of {net.stations} daily stations reported in the "
                f"last {WINDOW_DAYS} days, down from {net.prior} the week before"
            )
    return networks


def table(networks: dict[str, Network], today: date) -> str:
    rows = sorted(networks.values(), key=lambda n: (not n.reasons, n.client))
    lines = [
        f"### Network freshness ({today})",
        "",
        "| network | daily stations | reported last 7 d | week before | newest row | flag |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for n in rows:
        flag = ("🔴 " + "; ".join(n.reasons)) if n.reasons else ""
        lines.append(
            f"| {n.client} | {n.stations} | {n.recent} | {n.prior} | "
            f"{n.newest or 'never'} | {flag} |"
        )
    return "\n".join(lines)


# ── GitHub ────────────────────────────────────────────────────────────────────


class GitHub:
    """The REST calls this script needs, with the standard library only."""

    def __init__(self, repo: str, token: str) -> None:
        self.api = f"https://api.github.com/repos/{repo}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.api}{path}", data=data, method=method, headers=self.headers
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def ensure_label(self) -> None:
        try:
            self._call(
                "POST",
                "/labels",
                {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            )
        except Exception:  # noqa: BLE001, S110 — 422: it already exists
            pass

    def open_issues(self) -> list[dict]:
        return self._call("GET", f"/issues?state=open&labels={LABEL}&per_page=100")

    def create(self, title: str, body: str) -> dict:
        return self._call(
            "POST", "/issues", {"title": title, "body": body, "labels": [LABEL]}
        )

    def update(self, number: int, **fields) -> dict:
        return self._call("PATCH", f"/issues/{number}", fields)

    def comment(self, number: int, body: str) -> dict:
        return self._call("POST", f"/issues/{number}/comments", {"body": body})


def _issue_body(net: Network, today: date, run_url: str | None) -> str:
    return "\n".join(
        [
            MARKER.format(client=net.client),
            "",
            f"The daily archive is not getting data for **`{net.client}`**: "
            + "; ".join(net.reasons)
            + ".",
            "",
            f"- daily stations listed: {net.stations}",
            f"- reported in the last {WINDOW_DAYS} days: {net.recent}",
            f"- reported in the {WINDOW_DAYS} days before: {net.prior}",
            f"- newest row in any of its CSVs: {net.newest}",
            "",
            f"Checked {today}" + (f" by {run_url}" if run_url else "") + ". "
            "Look first at that run's `fetch-data` job for this network and at "
            "the station builder's log (a source listing fewer stations is "
            "logged there, then carried forward). This issue is updated by "
            "each daily run and closed when the data flows again.",
        ]
    )


def report(
    networks: dict[str, Network],
    github: GitHub,
    today: date,
    run_url: str | None = None,
    log=print,
) -> list[str]:
    github.ensure_label()
    existing = {}
    for issue in github.open_issues():
        body = issue.get("body") or ""
        if "<!-- gsn-freshness:" in body:
            client = body.split("<!-- gsn-freshness:", 1)[1].split("-->", 1)[0]
            existing[client.strip()] = issue
    done = []
    for net in sorted(networks.values(), key=lambda n: n.client):
        issue = existing.get(net.client)
        if net.reasons:
            title = f"No fresh data from {net.client}: {net.reasons[0]}"
            body = _issue_body(net, today, run_url)
            if issue is None:
                created = github.create(title, body)
                done.append(f"opened #{created['number']}: {title}")
            elif issue.get("title") != title:
                github.update(issue["number"], title=title, body=body)
                done.append(f"updated #{issue['number']}: {title}")
            else:
                github.update(issue["number"], body=body)
        elif issue is not None and net.recent:
            github.comment(
                issue["number"],
                f"Recovered: {net.recent} of {net.stations} daily stations "
                f"reported in the {WINDOW_DAYS} days to {today}. Closing.",
            )
            github.update(issue["number"], state="closed", state_reason="completed")
            done.append(f"closed #{issue['number']}: {net.client}")
    for line in done:
        log(line)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--stale-days", type=int, default=WINDOW_DAYS)
    parser.add_argument("--inventory", type=Path, default=INVENTORY)
    parser.add_argument("--stations-dir", type=Path, default=STATIONS_DIR)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    features = json.loads(args.inventory.read_text(encoding="utf-8"))["features"]
    networks = assess(
        features, args.stations_dir, args.today, stale_days=args.stale_days
    )
    text = table(networks, args.today)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    for net in networks.values():
        for reason in net.reasons:
            print(f"::error title=Stale network {net.client}::{reason}")

    token = os.environ.get("GITHUB_TOKEN")
    if args.dry_run or not token or not args.repo:
        return 0
    try:
        report(networks, GitHub(args.repo, token), args.today, args.run_url)
    except Exception as exc:  # noqa: BLE001 — reporting must not fail the run
        print(f"::warning title=Network freshness::could not update issues: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
