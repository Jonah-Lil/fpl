#!/usr/bin/env python3
"""
Last Man Standing — schedule allocator and life ledger for a FPL H2H league.

Reads the CSVs produced by fpl_h2h.py and works out, week by week, who lost a
life and who is still standing.

Two commands:

    # once, after GW1 — generates and freezes the season schedule
    python3 lms.py allocate --data ~/fpl-data --seed 683481

    # after every gameweek — recomputes the ledger from results
    python3 lms.py run --data ~/fpl-data

Outputs (into --data):
    lms_schedule.csv    each GW's elimination type, frozen at allocation time
    lms_ledger.csv      one row per manager per GW: score, margin, lives, eliminations
    lms_standings.csv   current LMS table
    lms_schedule.json   the frozen schedule + seed, for audit

Rules implemented
-----------------
* Everyone starts with 3 lives.
* Each gameweek the lowest-scoring manager who still has lives loses one.
  Eliminated managers are ignored when finding the lowest score.
* Score = gameweek points NET of transfer hits (FPL's own H2H basis).
* Gameweeks from GW2 may be double or triple elimination, allocated randomly
  from a published seed. Doubles + triples may never outnumber single weeks.
* Bomb Week: every manager who loses their H2H matchup loses a life, and the
  normal lowest-score elimination does not run that week. Only allocated when
  the life count cannot otherwise be cleared (league of 26+).
* Ties on the elimination score are broken by worst H2H points differential,
  applied independently at the 2nd- and 3rd-lowest positions too.
* A week never takes more lives than there are managers alive, and never
  reduces the whole field to zero — at least one manager always survives.
* The competition ends the moment one manager is the last with lives.

Capacity
--------
A single week removes 1 life, a double 2, a triple 3. Summed over the season
that is the schedule's *capacity*. Once 3N-1 lives are gone, one life remains
and it belongs to one manager, so a single survivor is forced. The allocator
therefore sizes capacity to exactly 3N-1 and paces it to land on GW38.

No third-party dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from typing import Dict, List, Optional, Tuple

TOTAL_GWS = 38
STARTING_LIVES = 3
SINGLE, DOUBLE, TRIPLE, BOMB = "single", "double", "triple", "bomb"
KILLS = {SINGLE: 1, DOUBLE: 2, TRIPLE: 3}


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

def required_capacity(n_managers: int, lives: int = STARTING_LIVES) -> int:
    """Lives that must be removable to force a single survivor."""
    return lives * n_managers - 1


def feasible_mixes(extra: int, max_specials: int) -> List[Tuple[int, int]]:
    """
    All (doubles, triples) pairs giving `extra` lives above the all-singles
    baseline, where each double adds 1 and each triple adds 2.

    Constrained by doubles + triples <= max_specials (the league's own rule
    that specials may not outnumber single weeks).
    """
    out = []
    for t in range(extra // 2 + 1):
        d = extra - 2 * t
        if d < 0:
            continue
        if d + t <= max_specials:
            out.append((d, t))
    return out


def allocate_schedule(
    n_managers: int,
    seed: int,
    total_gws: int = TOTAL_GWS,
    lives: int = STARTING_LIVES,
) -> dict:
    """
    Build the season schedule. Deterministic for a given (n_managers, seed).

    Returns a dict with the per-gameweek plan plus the arithmetic behind it,
    so the league can check the working.
    """
    rng = random.Random(seed)
    target = required_capacity(n_managers, lives)

    # Specials may not outnumber singles: doubles + triples <= singles.
    # A bomb week is neither, so it shrinks the pool both sides are drawn from.
    max_specials = total_gws // 2

    plan = {gw: SINGLE for gw in range(1, total_gws + 1)}
    notes: List[str] = []
    bomb_gw: Optional[int] = None

    extra = target - total_gws  # lives needed above the all-singles baseline

    if extra <= 0:
        notes.append(
            f"All-single season: {total_gws} single weeks remove {total_gws} lives, "
            f"which already covers the {target} needed. Expect a winner around "
            f"GW{target} rather than GW{total_gws}."
        )
        doubles = triples = 0
    else:
        mixes = feasible_mixes(extra, max_specials)
        needs_bomb = not mixes

        if needs_bomb:
            # Capacity ceiling breached (N >= 26). One week becomes a bomb, so
            # only total_gws - 1 weeks remain for the singles/specials ratio.
            max_specials = (total_gws - 1) // 2
            doubles, triples = 0, max_specials
            shortfall = target - (total_gws - 1 + triples * 2)
            notes.append(
                f"{target} lives exceed the {total_gws + (total_gws // 2) * 2} ceiling "
                f"reachable without a bomb. Allocated {triples} triple weeks plus one "
                f"Bomb Week to cover the remaining ~{shortfall} lives."
            )
        else:
            doubles, triples = rng.choice(mixes)
            notes.append(
                f"Need {extra} lives above the {total_gws}-life all-singles baseline; "
                f"chose {doubles} double and {triples} triple weeks from "
                f"{len(mixes)} valid combinations."
            )

        if needs_bomb:
            # Place the bomb first, in the last third where it does most work,
            # so the specials get placed around it.
            bomb_gw = rng.choice(range(int(total_gws * 0.66), total_gws))
            plan[bomb_gw] = BOMB

        # Place specials in GW2..GW38, never scheduling more eliminations than
        # the field can guarantee having alive at that point.
        slots = [gw for gw in range(2, total_gws + 1) if plan[gw] == SINGLE]
        rng.shuffle(slots)
        plan.update(_place_specials(slots, doubles, triples, n_managers, lives))

    counts = {k: sum(1 for v in plan.values() if v == k)
              for k in (SINGLE, DOUBLE, TRIPLE, BOMB)}
    capacity = sum(KILLS.get(v, 0) for v in plan.values())

    # A bomb removes a life from every alive manager who lost their H2H —
    # about half the field, once. Counted separately because it is
    # results-dependent, not fixed like a double or triple.
    bomb_capacity = (n_managers // 2) if bomb_gw is not None else 0
    if bomb_gw is not None:
        notes.append(
            f"plus a Bomb Week (~{bomb_capacity} lives, results-dependent)"
        )

    resolvable = capacity + bomb_capacity >= target
    if not resolvable:
        notes.append(
            f"UNRESOLVABLE: {target} lives must go, but {n_managers} managers can only "
            f"lose ~{capacity + bomb_capacity} under the current rules. The season can "
            f"end with several managers alive. Fix by allowing more than one Bomb Week, "
            f"relaxing the 'doubles + triples <= singles' cap, or starting with 2 lives "
            f"instead of {lives}."
        )

    return {
        "seed": seed,
        "n_managers": n_managers,
        "starting_lives": lives,
        "total_lives": lives * n_managers,
        "target_capacity": target,
        "scheduled_capacity": capacity,
        "bomb_capacity_estimate": bomb_capacity,
        "resolvable": resolvable,
        "counts": counts,
        "bomb_gw": bomb_gw,
        "plan": plan,
        "notes": notes,
    }


def _place_specials(
    slots: List[int], doubles: int, triples: int,
    n_managers: int, lives: int,
) -> Dict[int, str]:
    """
    Drop doubles and triples into shuffled gameweek slots, skipping any slot
    where the guaranteed-alive count could be below the eliminations needed.

    Guaranteed alive at week w is n - floor(capacity_before_w / lives): a
    manager needs `lives` hits to go out, so no more than that many can have
    been eliminated however the hits fell.
    """
    want = [TRIPLE] * triples + [DOUBLE] * doubles
    placed: Dict[int, str] = {}

    for kind in want:
        need = KILLS[kind]
        for i, gw in enumerate(slots):
            cap_before = sum(
                KILLS[placed.get(g, SINGLE)] for g in range(1, gw)
            )
            guaranteed_alive = n_managers - cap_before // lives
            if guaranteed_alive >= need:
                placed[gw] = kind
                slots.pop(i)
                break
        else:
            # No safe slot left; leave it as a single. The capacity check in
            # allocate_schedule surfaces any resulting shortfall.
            pass

    return placed


# --------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------

def _sort_key(row: dict) -> tuple:
    """
    Elimination order: lowest net score first. Ties broken by worst H2H points
    differential (most negative), then by lowest season points to date, then
    by entry id so the result is always deterministic.
    """
    return (
        row["net_points"],
        row["margin"] if row["margin"] is not None else 0,
        row.get("total_points") or 0,
        row["entry"],
    )


def run_ledger(
    weeks: Dict[int, List[dict]],
    plan: Dict[int, str],
    lives: int = STARTING_LIVES,
) -> Tuple[List[dict], Dict[int, int], Optional[int], List[int]]:
    """
    Walk the season gameweek by gameweek.

    `weeks` maps gameweek -> list of per-manager dicts with keys
    entry, entry_name, player_name, net_points, margin, total_points, h2h_result.

    Returns (ledger rows, lives by entry, winning entry or None, elimination order).
    """
    all_entries = {r["entry"]: r for rows in weeks.values() for r in rows}
    lives_left = {e: lives for e in all_entries}
    elimination_order: List[int] = []
    ledger: List[dict] = []
    winner: Optional[int] = None

    for gw in sorted(weeks):
        if winner is not None:
            break

        rows = weeks[gw]
        alive = [r for r in rows if lives_left.get(r["entry"], 0) > 0]
        if not alive:
            break

        kind = plan.get(gw, SINGLE)

        if kind == BOMB:
            losers = [r for r in alive if r.get("h2h_result") == "L"]
            # Never wipe the field out entirely.
            if losers and len(losers) == len(alive) and all(
                lives_left[r["entry"]] == 1 for r in losers
            ):
                losers = sorted(losers, key=_sort_key)[:-1]
            hit = losers
            applied = kind
        else:
            k = KILLS[kind]
            k = min(k, len(alive))
            order = sorted(alive, key=_sort_key)
            hit = order[:k]
            # If this would reduce every alive manager to zero, take one fewer.
            while hit and len(hit) == len(alive) and all(
                lives_left[r["entry"]] == 1 for r in hit
            ):
                hit = hit[:-1]
            applied = {1: SINGLE, 2: DOUBLE, 3: TRIPLE}.get(len(hit), SINGLE)

        hit_ids = {r["entry"] for r in hit}
        rank_by_entry = {
            r["entry"]: i + 1 for i, r in enumerate(sorted(alive, key=_sort_key))
        }

        for r in rows:
            eid = r["entry"]
            before = lives_left.get(eid, 0)
            lost = 1 if eid in hit_ids else 0
            after = max(0, before - lost)
            lives_left[eid] = after
            if before > 0 and after == 0:
                elimination_order.append(eid)
            ledger.append({
                "event": gw,
                "week_type": kind,
                "week_type_applied": applied,
                "entry": eid,
                "entry_name": r.get("entry_name"),
                "player_name": r.get("player_name"),
                "net_points": r["net_points"],
                "margin": r["margin"],
                "h2h_result": r.get("h2h_result"),
                "low_score_rank": rank_by_entry.get(eid),
                "was_alive": before > 0,
                "lives_before": before,
                "lost_life": bool(lost),
                "lives_after": after,
                "eliminated_this_gw": before > 0 and after == 0,
            })

        still_alive = [e for e, v in lives_left.items() if v > 0]
        if len(still_alive) == 1:
            winner = still_alive[0]

    return ledger, lives_left, winner, elimination_order


# --------------------------------------------------------------------------
# CSV plumbing
# --------------------------------------------------------------------------

def read_csv(path: str) -> List[dict]:
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} — run fpl_h2h.py first")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _num(v, cast=int):
    if v is None or v == "":
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def load_weeks(data_dir: str) -> Dict[int, List[dict]]:
    """
    Join results_long.csv (H2H margin, result) with manager_gameweeks.csv
    (net-of-hits score) into per-gameweek rows.
    """
    results = read_csv(os.path.join(data_dir, "results_long.csv"))
    gws = read_csv(os.path.join(data_dir, "manager_gameweeks.csv"))

    net = {(_num(r["entry"]), _num(r["event"])): _num(r["net_points"]) for r in gws}
    totals = {(_num(r["entry"]), _num(r["event"])): _num(r["total_points"]) for r in gws}

    weeks: Dict[int, List[dict]] = {}
    for r in results:
        if str(r.get("played")).lower() != "true":
            continue
        entry, ev = _num(r["entry"]), _num(r["event"])
        score = net.get((entry, ev))
        if score is None:  # fall back to the H2H points, already net of hits
            score = _num(r["points_for"])
        if score is None:
            continue
        weeks.setdefault(ev, []).append({
            "entry": entry,
            "entry_name": r.get("entry_name"),
            "player_name": r.get("player_name"),
            "net_points": score,
            "margin": _num(r["margin"]),
            "total_points": totals.get((entry, ev)),
            "h2h_result": r.get("result"),
        })
    return weeks


def write_csv(path: str, rows: List[dict]) -> None:
    if not rows:
        print(f"  (skipped {os.path.basename(path)} — no rows)")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)
    print(f"  wrote {os.path.basename(path):<22} {len(rows):>5} rows")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_allocate(args) -> int:
    sched_path = os.path.join(args.data, "lms_schedule.json")
    if os.path.exists(sched_path) and not args.overwrite:
        raise SystemExit(
            f"{sched_path} already exists. The schedule is meant to be frozen "
            "once published — pass --overwrite only if you really mean it."
        )

    n = args.managers
    if n is None:
        standings = read_csv(os.path.join(args.data, "standings.csv"))
        n = len(standings)
    if n < 2:
        raise SystemExit(f"need at least 2 managers to allocate a schedule (found {n})")

    sched = allocate_schedule(n, args.seed)

    print(f"Last Man Standing — {n} managers, seed {args.seed}")
    print(f"  total lives ........ {sched['total_lives']}")
    print(f"  capacity needed .... {sched['target_capacity']}  (3N-1, forces one survivor)")
    print(f"  capacity scheduled . {sched['scheduled_capacity']}")
    c = sched["counts"]
    print(f"  weeks .............. {c[SINGLE]} single, {c[DOUBLE]} double, "
          f"{c[TRIPLE]} triple, {c[BOMB]} bomb")
    for note in sched["notes"]:
        print(f"  note: {note}")
    if not sched["resolvable"]:
        print("  WARNING: this league cannot be resolved under the current rules "
              "(see note above).")

    payload = {k: v for k, v in sched.items() if k != "plan"}
    payload["plan"] = {str(k): v for k, v in sched["plan"].items()}
    with open(sched_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    write_csv(os.path.join(args.data, "lms_schedule.csv"),
              [{"event": gw, "week_type": sched["plan"][gw],
                "lives_removed": KILLS.get(sched["plan"][gw], "varies")}
               for gw in sorted(sched["plan"])])
    print(f"Schedule frozen in {sched_path}")
    return 0


def cmd_run(args) -> int:
    sched_path = os.path.join(args.data, "lms_schedule.json")
    if not os.path.exists(sched_path):
        raise SystemExit("no lms_schedule.json — run `lms.py allocate` after GW1 first")
    with open(sched_path, encoding="utf-8") as f:
        sched = json.load(f)
    plan = {int(k): v for k, v in sched["plan"].items()}

    weeks = load_weeks(args.data)
    if not weeks:
        print("No played gameweeks yet — nothing to compute.")
        return 0

    ledger, lives_left, winner, order = run_ledger(weeks, plan)

    names = {r["entry"]: (r.get("entry_name"), r.get("player_name")) for r in ledger}
    out_rank = {e: i for i, e in enumerate(order)}
    standings = sorted(
        ({"entry": e,
          "entry_name": names.get(e, (None, None))[0],
          "player_name": names.get(e, (None, None))[1],
          "lives_left": v,
          "status": "WINNER" if e == winner else ("alive" if v > 0 else "eliminated"),
          "eliminated_gw": next(
              (r["event"] for r in ledger
               if r["entry"] == e and r["eliminated_this_gw"]), None),
          "lives_lost": sum(1 for r in ledger if r["entry"] == e and r["lost_life"]),
          } for e, v in lives_left.items()),
        key=lambda r: (-r["lives_left"], -(out_rank.get(r["entry"], -1))),
    )
    for i, r in enumerate(standings, 1):
        r["position"] = i
    standings = [{"position": r.pop("position"), **r} for r in standings]

    write_csv(os.path.join(args.data, "lms_ledger.csv"), ledger)
    write_csv(os.path.join(args.data, "lms_standings.csv"), standings)

    played = max(weeks)
    alive = [r for r in standings if r["lives_left"] > 0]
    print(f"\nLMS through GW{played} — {len(alive)} of {len(standings)} still standing")
    for r in standings[:12]:
        tag = f"{r['lives_left']} lives" if r["lives_left"] else f"out GW{r['eliminated_gw']}"
        print(f"  {r['position']:>2}. {(r['entry_name'] or '')[:22]:<22} {tag}")
    if winner:
        runner_up = order[-1] if order else None
        print(f"\n  WINNER: {names.get(winner, ('?',))[0]}")
        if runner_up:
            print(f"  Runner-up: {names.get(runner_up, ('?',))[0]} "
                  f"(last eliminated, GW{next(r['event'] for r in ledger if r['entry'] == runner_up and r['eliminated_this_gw'])})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Last Man Standing for a FPL H2H league.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("allocate", help="generate and freeze the season schedule")
    a.add_argument("--data", default="./data")
    a.add_argument("--seed", type=int, required=True,
                   help="publish this to your league so the draw is auditable")
    a.add_argument("--managers", type=int, default=None,
                   help="override the manager count (default: read standings.csv)")
    a.add_argument("--overwrite", action="store_true")
    a.set_defaults(func=cmd_allocate)

    r = sub.add_parser("run", help="recompute the ledger from results")
    r.add_argument("--data", default="./data")
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
