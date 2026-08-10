#!/usr/bin/env python3
"""
Rebuild the FPL H2H dashboard.

Fetches live data from the public Fantasy Premier League API, computes the
league table, weekly results, position history and luck ratings, then rewrites
the <script id="data"> JSON block inside index.html in place.

Rewriting in place (rather than regenerating the whole page from a template)
means you can restyle index.html however you like and the build keeps working,
as long as the data block stays put.

Standard library only - no pip install needed in CI.

Usage:
    python3 build.py                     # fetch live, patch ./index.html
    python3 build.py --league 683481
    python3 build.py --fixtures ./tests/fixtures   # offline, for testing
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://fantasy.premierleague.com/api"

# The FPL API rejects the default urllib user agent with a 403.
UA = "Mozilla/5.0 (compatible; fpl-dashboard/1.0; +https://github.com/Jonah-Lil/fpl)"

WIN, DRAW = 3, 1


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class FetchError(RuntimeError):
    pass


def get_json(path: str, *, retries: int = 4, timeout: int = 30):
    """GET {API}/{path} and parse JSON, retrying on transient failures."""
    url = f"{API}/{path}"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                json.JSONDecodeError, OSError) as exc:
            last = exc
            # 404 means the resource genuinely is not there; do not retry.
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                break
            if attempt < retries - 1:
                sleep = 2 ** attempt
                print(f"  retry {attempt + 1}/{retries - 1} for {path} "
                      f"after {type(exc).__name__}: {exc} (sleeping {sleep}s)",
                      file=sys.stderr)
                time.sleep(sleep)
    raise FetchError(f"could not fetch {url}: {last}")


class Fixtures:
    """Offline stand-in for the API, for testing without network access."""

    def __init__(self, directory: Path):
        self.dir = directory

    def __call__(self, path: str, **_kw):
        name = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") + ".json"
        f = self.dir / name
        if not f.exists():
            raise FetchError(f"no fixture {f} for API path {path}")
        return json.loads(f.read_text())


fetch = get_json  # swapped for a Fixtures instance by --fixtures


# --------------------------------------------------------------------------
# Data collection
# --------------------------------------------------------------------------

def league_standings(league_id: int) -> dict:
    """Page through H2H standings and new entries.

    Before the season starts, members sit in `new_entries` and `standings`
    is empty. After GW1 they move across. We need both, or the dashboard
    shows nobody during preseason.
    """
    meta, standings, new_entries = None, [], []

    page = 1
    while True:
        d = fetch(f"leagues-h2h/{league_id}/standings/?page_standings={page}")
        meta = meta or d["league"]
        standings.extend(d["standings"]["results"])
        if not d["standings"].get("has_next"):
            break
        page += 1

    page = 1
    while True:
        d = fetch(f"leagues-h2h/{league_id}/standings/?page_new_entries={page}")
        new_entries.extend(d["new_entries"]["results"])
        if not d["new_entries"].get("has_next"):
            break
        page += 1

    return {"league": meta, "standings": standings, "new_entries": new_entries}


def league_matches(league_id: int) -> list[dict]:
    out, page = [], 1
    while True:
        d = fetch(f"leagues-h2h-matches/league/{league_id}/?page={page}")
        out.extend(d["results"])
        if not d.get("has_next"):
            break
        page += 1
    return out


def gameweeks() -> tuple[int | None, int | None, set[int]]:
    """Return (current_gw, next_gw, set of finished gameweeks)."""
    events = fetch("bootstrap-static/")["events"]
    current = next((e["id"] for e in events if e.get("is_current")), None)
    nxt = next((e["id"] for e in events if e.get("is_next")), None)
    finished = {e["id"] for e in events if e.get("finished")}
    return current, nxt, finished


# --------------------------------------------------------------------------
# Derived stats
# --------------------------------------------------------------------------

def collect_managers(league: dict) -> dict[str, dict]:
    """Build the manager map from standings + new entries, keyed by entry id."""
    managers: dict[str, dict] = {}

    for r in league["standings"]:
        eid = str(r["entry"])
        managers[eid] = {
            "entry": eid,
            "entry_name": r.get("entry_name") or "?",
            "player_name": r.get("player_name") or "",
            "h2h_rank": r.get("rank"),
            "h2h_points": r.get("total", 0),
            "played": r.get("matches_played", 0),
            "won": r.get("matches_won", 0),
            "drawn": r.get("matches_drawn", 0),
            "lost": r.get("matches_lost", 0),
            "points_for": r.get("points_for", 0),
        }

    for r in league["new_entries"]:
        eid = str(r["entry"])
        if eid in managers:
            continue
        first = r.get("player_first_name") or ""
        last = r.get("player_last_name") or ""
        managers[eid] = {
            "entry": eid,
            "entry_name": r.get("entry_name") or "?",
            "player_name": f"{first} {last}".strip(),
            "h2h_rank": None, "h2h_points": 0, "played": 0,
            "won": 0, "drawn": 0, "lost": 0, "points_for": 0,
        }

    for m in managers.values():
        m.setdefault("total_points", None)
        m.setdefault("overall_rank", None)
        m.setdefault("squad_value", None)
        m.setdefault("next_opponent", None)
        for k in ("lives_left", "lms_status", "lms_eliminated_gw"):
            m.setdefault(k, None)

    return managers


def enrich_from_entries(managers: dict[str, dict]) -> dict[str, dict[int, int]]:
    """Pull total points, overall rank and squad value per manager.

    Also returns {entry: {gameweek: season total to date}}, which LMS uses as
    a tiebreak when two managers post the same score in the same week.
    """
    histories: dict[str, dict[int, int]] = {}
    for eid, m in managers.items():
        try:
            hist = fetch(f"entry/{eid}/history/")
        except FetchError as exc:
            print(f"  warning: no history for entry {eid}: {exc}", file=sys.stderr)
            continue
        current = hist.get("current") or []
        if not current:
            continue
        histories[eid] = {
            r["event"]: r.get("total_points") for r in current if r.get("event")
        }
        last = current[-1]
        m["total_points"] = last.get("total_points")
        m["overall_rank"] = last.get("overall_rank")
        value = last.get("value")
        if value:
            m["squad_value"] = round(value / 10, 1)
    return histories


def sides(match: dict):
    """Yield (entry_id, name, score, h2h_pts) for each side of a match.

    Byes are played against 'AVERAGE', which has a null entry id.
    """
    for n in (1, 2):
        entry = match.get(f"entry_{n}_entry")
        yield (
            str(entry) if entry else None,
            match.get(f"entry_{n}_name") or "AVERAGE",
            match.get(f"entry_{n}_points") or 0,
            match.get(f"entry_{n}_total") or 0,
        )


def weekly_results(matches: list[dict], finished: set[int]) -> dict[str, list]:
    out: dict[str, list] = {}
    for m in matches:
        gw = m.get("event")
        if gw not in finished:
            continue
        (_, n1, s1, _), (_, n2, s2, _) = sides(m)
        out.setdefault(str(gw), []).append({"e1": n1, "s1": s1, "e2": n2, "s2": s2})
    return out


def position_history(matches: list[dict], managers: dict, finished: set[int]) -> dict:
    """Recompute the H2H table after each finished gameweek.

    The API has no historical rank endpoint, so we replay the season.
    """
    gws = sorted({m["event"] for m in matches if m.get("event") in finished})
    if not gws:
        return {}

    pts = {eid: 0 for eid in managers}
    pf = {eid: 0 for eid in managers}
    history: dict[str, list[int]] = {eid: [] for eid in managers}

    for gw in gws:
        for m in matches:
            if m.get("event") != gw:
                continue
            for eid, _name, score, h2h in sides(m):
                if eid in pts:
                    pts[eid] += h2h
                    pf[eid] += score

        order = sorted(managers, key=lambda e: (-pts[e], -pf[e]))
        for rank, eid in enumerate(order, start=1):
            history[eid].append(rank)

    return history


def luck_table(matches: list[dict], managers: dict, finished: set[int]) -> list[dict]:
    """Points won vs points deserved.

    Expected = what you'd average playing the entire league each week,
    instead of the one opponent the fixture list handed you.
    """
    gws = sorted({m["event"] for m in matches if m.get("event") in finished})
    if not gws:
        return []

    actual = {eid: 0 for eid in managers}
    expected = {eid: 0.0 for eid in managers}
    played: set[str] = set()

    for gw in gws:
        scores: dict[str, int] = {}
        for m in matches:
            if m.get("event") != gw:
                continue
            for eid, _name, score, h2h in sides(m):
                if eid in actual:
                    actual[eid] += h2h
                    scores[eid] = score
                    played.add(eid)

        if len(scores) < 2:
            continue

        for eid, s in scores.items():
            others = [v for k, v in scores.items() if k != eid]
            wins = sum(1 for v in others if s > v)
            draws = sum(1 for v in others if s == v)
            expected[eid] += (WIN * wins + DRAW * draws) / len(others)

    rows = []
    for eid, m in managers.items():
        # Include anyone who has played, even on 0 actual and 0 expected -
        # a manager who deserved nothing and got nothing still belongs in
        # the table. Only managers with no fixtures yet are excluded.
        if eid not in played:
            continue
        rows.append({
            "entry": eid,
            "entry_name": m["entry_name"],
            "actual": actual[eid],
            "expected": round(expected[eid], 2),
            "luck": round(actual[eid] - expected[eid], 2),
        })
    rows.sort(key=lambda r: -r["luck"])
    return rows


def attach_next_opponent(managers: dict, matches: list[dict], next_gw) -> None:
    if not next_gw:
        return
    for m in matches:
        if m.get("event") != next_gw:
            continue
        (e1, n1, _, _), (e2, n2, _, _) = sides(m)
        if e1 in managers:
            managers[e1]["next_opponent"] = {"entry": e2, "name": n2}
        if e2 in managers:
            managers[e2]["next_opponent"] = {"entry": e1, "name": n1}


# --------------------------------------------------------------------------
# Last Man Standing
# --------------------------------------------------------------------------

def lms_weeks(matches: list[dict], managers: dict, finished: set[int],
              histories: dict) -> dict[int, list[dict]]:
    """Reshape H2H matches into the per-gameweek rows lms.py expects.

    lms.py normally reads this from CSVs written by fpl_h2h.py. We already
    have the same data in memory, so we hand it over directly and skip the
    CSV round trip entirely.

    Score is the H2H points_for from the match, which is FPL's own basis and
    is already net of transfer hits - exactly what the LMS rules call for.
    """
    weeks: dict[int, list[dict]] = {}
    for m in matches:
        gw = m.get("event")
        if gw not in finished:
            continue
        pair = list(sides(m))
        for i, (eid, name, score, _h2h) in enumerate(pair):
            if eid not in managers:
                continue  # the AVERAGE opponent is not a league member
            _, _, opp_score, _ = pair[1 - i]
            weeks.setdefault(gw, []).append({
                "entry": int(eid),
                "entry_name": name,
                "player_name": managers[eid]["player_name"],
                "net_points": score,
                "margin": score - opp_score,
                "total_points": (histories.get(eid) or {}).get(gw),
                "h2h_result": "W" if score > opp_score else ("D" if score == opp_score else "L"),
            })
    return weeks


def merge_lms(payload: dict, managers: dict, matches: list[dict],
              finished: set[int], histories: dict, repo_dir: Path) -> None:
    """Run the Last Man Standing ledger and fold it into the payload.

    Needs lms_schedule.json, which lms.py writes once via `allocate`. The
    schedule is deliberately frozen at allocation time so the draw stays
    auditable, so this never generates one on the fly.
    """
    sched_path = repo_dir / "lms_schedule.json"
    if not sched_path.exists():
        print("  LMS: no lms_schedule.json yet - skipping (run the "
              "'Allocate LMS schedule' workflow after GW1)")
        return

    try:
        import lms  # noqa: PLC0415 - optional, only needed once allocated
    except ImportError:
        print("  warning: lms_schedule.json exists but lms.py is missing",
              file=sys.stderr)
        return

    try:
        sched = json.loads(sched_path.read_text())
        plan = {int(k): v for k, v in sched["plan"].items()}
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        print(f"  warning: lms_schedule.json is unreadable, skipping LMS: {exc}",
              file=sys.stderr)
        return

    # Show the elimination strip from day one, even before a ball is kicked.
    payload["lms_schedule"] = [
        {"event": gw, "type": plan[gw]} for gw in sorted(plan)
    ]

    weeks = lms_weeks(matches, managers, finished, histories)
    if not weeks:
        print("  LMS: schedule loaded, no gameweeks played yet")
        return

    ledger, lives_left, winner, _order = lms.run_ledger(weeks, plan)

    standings = []
    for row in sorted(
        lives_left.items(),
        key=lambda kv: (-kv[1], -(next(
            (r["event"] for r in ledger
             if r["entry"] == kv[0] and r["eliminated_this_gw"]), 0))),
    ):
        eid, lives = row
        out_gw = next((r["event"] for r in ledger
                       if r["entry"] == eid and r["eliminated_this_gw"]), None)
        name = next((r["entry_name"] for r in ledger if r["entry"] == eid), None)
        standings.append({
            "entry": str(eid),
            "entry_name": name,
            "position": len(standings) + 1,
            "lives_left": lives,
            "status": "WINNER" if eid == winner else ("alive" if lives else "eliminated"),
            "eliminated_gw": out_gw,
        })
    payload["lms_standings"] = standings

    events: dict[str, list] = {}
    for r in ledger:
        if r["lost_life"]:
            events.setdefault(str(r["event"]), []).append({
                "name": r["entry_name"],
                "score": r["net_points"],
                "out": r["eliminated_this_gw"],
            })
    payload["lms_events"] = events

    for row in standings:
        m = managers.get(row["entry"])
        if m:
            m["lives_left"] = row["lives_left"]
            m["lms_status"] = row["status"]
            m["lms_eliminated_gw"] = row["eliminated_gw"]

    alive = sum(1 for r in standings if r["lives_left"] > 0)
    print(f"  LMS: {alive} of {len(standings)} still standing"
          + (f", WINNER {standings[0]['entry_name']}" if winner else ""))


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_payload(league_id: int) -> dict:
    print(f"fetching league {league_id} ...")
    league = league_standings(league_id)
    matches = league_matches(league_id)
    current_gw, next_gw, finished = gameweeks()
    print(f"  current GW {current_gw}, next GW {next_gw}, "
          f"{len(finished)} finished, {len(matches)} matches")

    managers = collect_managers(league)
    print(f"  {len(managers)} managers")
    if not managers:
        raise FetchError(
            f"league {league_id} returned no managers at all - "
            "check the league id and that the league is public"
        )

    histories = enrich_from_entries(managers)
    attach_next_opponent(managers, matches, next_gw)

    ordered = sorted(
        managers.values(),
        key=lambda m: (m["h2h_rank"] is None, m["h2h_rank"] or 0, m["entry_name"].lower()),
    )

    payload = {
        "league_name": league["league"]["name"],
        "league_id": str(league_id),
        "current_gw": current_gw,
        "next_gw": next_gw,
        "generated": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "managers": ordered,
        "weekly": weekly_results(matches, finished),
        "position_history": position_history(matches, managers, finished),
        "luck": luck_table(matches, managers, finished),
        "lms_standings": [],
        "lms_schedule": [],
        "lms_events": {},
    }
    merge_lms(payload, managers, matches, finished, histories,
              Path(__file__).parent)
    return payload


DATA_BLOCK = re.compile(
    r'(<script id="data" type="application/json">)(.*?)(</script>)',
    re.DOTALL,
)


def _significant(payload: dict) -> str:
    """Payload fingerprint ignoring the generated-at timestamp.

    Without this the file would change every single run and the repo would
    collect a commit an hour, 24 a day, forever - even in the off season.
    """
    return json.dumps({k: v for k, v in payload.items() if k != "generated"},
                      sort_keys=True, separators=(",", ":"))


def patch_html(html_path: Path, payload: dict) -> bool:
    """Replace the data block. Returns True if the file changed."""
    html = html_path.read_text(encoding="utf-8")
    match = DATA_BLOCK.search(html)
    if not match:
        raise SystemExit(
            f'{html_path} has no <script id="data" type="application/json"> block. '
            "build.py patches that block in place, so it must exist."
        )

    try:
        existing = json.loads(match.group(2))
    except json.JSONDecodeError:
        existing = None

    if existing is not None and _significant(existing) == _significant(payload):
        return False

    # separators avoids stray whitespace churn; </ is escaped so the JSON can
    # never terminate the surrounding <script> tag early.
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    blob = blob.replace("</", "<\\/")

    updated = DATA_BLOCK.sub(
        lambda m: m.group(1) + blob + m.group(3), html, count=1
    )
    if updated == html:
        return False
    html_path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild the FPL H2H dashboard.")
    ap.add_argument("--league", type=int,
                    default=int(os.environ.get("FPL_LEAGUE_ID", "683481")))
    ap.add_argument("--html", type=Path,
                    default=Path(__file__).parent / "index.html")
    ap.add_argument("--fixtures", type=Path,
                    help="read from saved JSON instead of the network (testing)")
    ap.add_argument("--count-managers", action="store_true",
                    help="print how many managers are in the league, then exit")
    args = ap.parse_args()

    if args.fixtures:
        global fetch
        fetch = Fixtures(args.fixtures)
        print(f"offline mode: reading fixtures from {args.fixtures}")

    # Used by the LMS allocation workflow so nobody has to count heads.
    if args.count_managers:
        try:
            print(len(collect_managers(league_standings(args.league))))
        except FetchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        payload = build_payload(args.league)
    except FetchError as exc:
        # Fail loudly and leave index.html untouched. A stale dashboard beats
        # a broken one, and a red X in the Actions tab is the signal we want.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    changed = patch_html(args.html, payload)
    print(f"{args.html.name}: {'updated' if changed else 'no change'} "
          f"({payload['generated']})")

    # Let the workflow decide whether to commit.
    step_output = os.environ.get("GITHUB_OUTPUT")
    if step_output:
        with open(step_output, "a") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
