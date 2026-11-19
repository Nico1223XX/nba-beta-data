#!/usr/bin/env python3
"""NBA backup collector, v2 -- runs on the GitHub Actions runner.

Runs on a 5-min cron. FIRST checks whether the local VM is alive by
reading the checked-out beta_latest.json's generated_at_bj. If the board
is fresher than stale_minutes (default 10, aligned with the liveness
alarm), exits WITHOUT collecting. Only when the board is stale does it
collect one snapshot of the 4 odds/company legs (ESPN, Kalshi,
Polymarket, ProphetX) and write gap_snapshot.json for upload as a
GitHub Actions artifact (retention 1 day). Collected DATA never enters
git history; the workflow makes ZERO git writes.

RISK CONTROLS (Nico, 2026-11-19 -- maximum risk avoidance):
- Kill switch: backup_config.json "enabled": false disables collection
  instantly (the collector exits with decision "disabled" before any
  fetch). The same flag disables the local merge job.
- Conservative threshold: stale_minutes 10 (was 8). A transient push
  hiccup under 10 min must NOT trigger a collection.
- NO DKNet leg in v1: Playwright on the runner is unverified, so the
  DK Network splits leg is DROPPED. v1 ships odds + company line only
  (ESPN/Kalshi/Polymarket/ProphetX). DKNet is a v2 candidate, re-added
  only after the runner leg is proven. Never ship an unverified
  splits leg.
- AN is skipped (blocked from runner IPs); ESPN covers the company line.
- Exchange cash (Kalshi/Polymarket perteam_state) is NOT collected:
  the production collectors walk trade history back to already-seen
  trades on recovery, so the cash leg self-heals without backup.
- The snapshot is a fill-only, same-source/same-convention record:
  Kalshi/Polymarket/ProphetX/ESPN values are identical regardless of
  fetcher. Every tick is flagged source="backup" by the merge job, and
  games with gap-filled data are flagged gap_filled=true and EXCLUDED
  from forward-validation samples until proven clean.
- The collector never writes to the production store and never touches
  beta_publish.py / beta_v2.py / MODEL.md / games.jsonl.

Force (test only): --force, or env BACKUP_FORCE_COLLECT=true (set by the
workflow's workflow_dispatch input), or config force_collect=true.
Forced runs collect regardless of freshness and set forced=true.

Usage: python3 nba_backup_collect.py [--config backup_config.json]
       [--repo-dir .] [--out gap_snapshot.json] [--dry-run] [--force]
Exits 0 always (a leg failure is recorded in the summary, never a
crash); a non-zero exit means the script itself crashed.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

BJ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
ABBR = {"WSH": "WAS", "SA": "SAS", "NO": "NOP", "GS": "GSW", "UTAH": "UTA", "NY": "NYK"}
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gamma-api.polymarket.com"
PX_BASE = "https://cash.api.prophetx.co/trade/public/api/v1"  # production host; api.prophetx.com is unreachable (DNS fail on runner, conn fail here)


def fetch_json(url, timeout=25, attempts=2):
    last = None
    for _ in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), None
        except Exception as e:  # noqa: BLE001
            last = str(e)[:100]
            time.sleep(2)
    return None, last


def dec_from_american(am):
    try:
        am = float(am)
    except Exception:
        return None
    if am > 0:
        return round(1 + am / 100.0, 4)
    if am < 0:
        return round(1 + 100.0 / abs(am), 4)
    return None


def gid_of(away, home):
    return "%s-%s" % (away, home)


# ---------------------------------------------------------------- ESPN
def espn_games(now_bj):
    """(events, err) -- scoreboard for US today and tomorrow.

    Each event carries "us_date": the US date it was fetched under.
    The US date is the key for EVERYTHING downstream -- the production
    game_id, the Kalshi event ticker, and the Polymarket slug all use
    the US date of tipoff (a game tipping 01:00 UTC Nov 20 is a Nov 19
    US game: game_id 20261119-..., ticker KXNBAGAME-26NOV19..., slug
    nba-...-2026-11-19). Using the UTC or Beijing date here silently
    matches nothing (Kalshi/Poly return empty, and the merge files the
    ticks under games_not_in_lib)."""
    today_us = (now_bj - timedelta(hours=13)).date()
    out, err = [], None
    for d in (today_us, today_us + timedelta(days=1)):
        ds = d.strftime("%Y%m%d")
        data, e = fetch_json("%s?dates=%s" % (ESPN_BASE, ds))
        if data is None:
            err = e
            continue
        for ev in data.get("events", []):
            try:
                comps = ev.get("competitions", [])
                if not comps:
                    continue
                comp = comps[0]
                dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                teams = comp.get("competitors", [])
                if len(teams) < 2:
                    continue
                home = next(t for t in teams if t.get("homeAway") == "home")
                away = next(t for t in teams if t.get("homeAway") == "away")
                ha = ABBR.get(home["team"]["abbreviation"], home["team"]["abbreviation"])
                aa = ABBR.get(away["team"]["abbreviation"], away["team"]["abbreviation"])
                out.append({
                    "game_id": ds + "-" + gid_of(aa, ha),
                    "away": aa, "home": ha,
                    "tipoff_utc": dt,
                    "tipoff_bj": dt.astimezone(BJ),
                    "us_date": d,
                    "status": ev.get("status", {}).get("type", {}).get("state", ""),
                    "odds": (comp.get("odds") or [None])[0],
                })
            except Exception:  # noqa: BLE001
                continue
    return out, err


def _american_odds(side):
    """American odds string from an ESPN moneyline side.

    Current shape: {"close": {"odds": "-243"}, "open": {"odds": "-334"}};
    older shape had a flat "summary". Prefer close, then summary, then open.
    """
    if not isinstance(side, dict):
        return None
    if side.get("summary"):
        return side["summary"]
    for key in ("close", "open"):
        node = side.get(key)
        if isinstance(node, dict) and node.get("odds"):
            return node["odds"]
    return None


def espn_close_odds(ev):
    """ESPN moneyline close odds -> (away_dec, home_dec, provider) or None."""
    o = ev.get("odds")
    if not o:
        return None
    try:
        ml = o.get("moneyline")
        if not ml:
            return None
        a = dec_from_american(_american_odds(ml.get("away", {})))
        h = dec_from_american(_american_odds(ml.get("home", {})))
        if a is None or h is None:
            return None
        return {"away": a, "home": h, "provider": o.get("provider", {}).get("name", "ESPN")}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- Kalshi
def kalshi_anchor(g):
    """Kalshi anchor (mid per team) or (None, err). Ticker uses the US date.

    Mirrors the production collector exactly: the price fields are the
    *_dollars / *_fp variants (already in dollars, e.g. 0.30 = 30%);
    the old cent fields (yes_bid/yes_ask/last_price) are null and must
    NOT be used. Team code = market ticker after the last "-" (e.g.
    KXNBAGAME-26NOV19GSWMEM-GSW -> GSW)."""
    ticker = "KXNBAGAME-%s%s%s" % (
        g["us_date"].strftime("%y%b%d").upper(),
        g["away"], g["home"])
    data, err = fetch_json(
        "%s/markets?event_ticker=%s&limit=10" % (KALSHI_BASE, ticker))
    if data is None:
        return None, err
    out = {}
    for m in data.get("markets", []):
        try:
            code = (m.get("ticker") or "").rsplit("-", 1)[-1]
            if code not in (g["away"], g["home"]):
                continue
            bid = m.get("yes_bid_dollars")
            ask = m.get("yes_ask_dollars")
            if bid is None or ask is None:
                continue
            bid, ask = float(bid), float(ask)
            out[code] = {"bid": bid, "ask": ask,
                         "mid": round((bid + ask) / 2, 4),
                         "last": float(m.get("last_price_dollars") or 0),
                         "open_interest": float(m.get("open_interest_fp") or 0),
                         "volume": float(m.get("volume_fp") or 0)}
        except Exception:  # noqa: BLE001
            continue
    return (out if out else None), (None if out else "no_teams")


# ---------------------------------------------------------------- Poly
def poly_anchor(g):
    """Polymarket anchor (winner-market prices) or (None, err). Slug uses the US date."""
    d = g["us_date"].strftime("%Y-%m-%d")
    slug = "nba-%s-%s-%s" % (g["away"].lower(), g["home"].lower(), d)
    data, err = fetch_json("%s/events?slug=%s" % (POLY_BASE, slug))
    if data is None:
        return None, err
    try:
        evs = data if isinstance(data, list) else []
        if not evs:
            return None, "no_event"
        mkts = evs[0].get("markets", [])
        if not mkts:
            return None, "no_markets"
        m0 = mkts[0]
        outcomes = json.loads(m0.get("outcomes", "[]"))
        prices = [float(x) for x in json.loads(m0.get("outcomePrices", "[]"))]
        if len(outcomes) != len(prices) or len(outcomes) < 2:
            return None, "bad_market"
        vol = None
        try:
            vol = float(m0.get("volume", 0))
        except Exception:  # noqa: BLE001
            pass
        return {"prices": dict(zip(outcomes, prices)), "volume": vol}, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:100]


# ---------------------------------------------------------------- ProphetX
def prophetx_events():
    """Full-pagination walk of Basketball/NBA events. (events, pages, err)."""
    now_ts = int(time.time())
    deadline = now_ts + 26 * 3600
    out, pages, err = [], 0, None
    nxt = now_ts
    while pages < 60:
        data, e = fetch_json(
            "%s/events?limit=100&next=%d" % (PX_BASE, nxt))
        pages += 1
        if data is None:
            err = e
            break
        # the API returns {"data": [events...], "next": ...}; tolerate a
        # dict-shaped {"data": {"events": [...]}} as well
        raw = data.get("data")
        if isinstance(raw, dict):
            evs = raw.get("events", []) or []
            nxt_val = raw.get("next")
        else:
            evs = raw or []
            nxt_val = data.get("next")
        if not evs:
            break
        last_ts = None
        for ev in evs:
            try:
                sched = ev.get("scheduled")
                if not sched:
                    continue
                ts = int(datetime.fromisoformat(
                    sched.replace("Z", "+00:00")).timestamp())
                last_ts = ts if last_ts is None else max(last_ts, ts)
                if (ev.get("sport") or {}).get("name") == "Basketball" and \
                        (ev.get("tournament") or {}).get("name") == "NBA":
                    out.append(ev)
            except Exception:  # noqa: BLE001
                continue
        if last_ts is None or last_ts > deadline:
            break
        # follow the API's own cursor when present, else the last timestamp
        try:
            nxt = int(nxt_val) if nxt_val is not None else last_ts
        except Exception:  # noqa: BLE001
            nxt = last_ts
        if nxt <= now_ts and last_ts is not None:
            nxt = last_ts
    return out, pages, err


def prophetx_moneyline(ev, gid):
    """Moneyline market -> (row-dict, err). Mirrors the production reader:
    best decimal per side = max dec, depth = sum(value)."""
    eid = ev.get("event_id")
    data, err = fetch_json("%s/events/%s/markets" % (PX_BASE, eid))
    if data is None:
        return None, err
    try:
        mkts = (data.get("data") or {}).get("markets", []) or []
        ml = next((m for m in mkts
                   if m.get("name") == "Moneyline" and m.get("status") == "active"),
                  None)
        if ml is None:
            return None, "no_moneyline"
        lines = ml.get("lines", [])
        home = next((ln for ln in lines if ln.get("is_home")), None)
        away = next((ln for ln in lines if not ln.get("is_home")), None)
        if home is None or away is None:
            return None, "no_sides"
        return {"home": home, "away": away}, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:100]


def prophetx_sides(ev):
    """(away_abbr, home_abbr) for a ProphetX event.

    Mirrors the production collector: match the event name
    ("Away at Home", full team names) against the competitors' names
    to get abbreviations; fall back to seq 1 = away, seq 0 = home.
    The gid MUST be abbreviations (e.g. GSW-MEM), not full names --
    beta_v2 groups ProphetX rows by gid, and a full-name gid matches
    nothing in the library."""
    name = ev.get("name") or ""
    comps = {}
    for c in ev.get("competitors", []) or []:
        comps[c.get("id")] = {"abbr": c.get("abbreviation"),
                              "name": c.get("name"),
                              "seq": c.get("seq")}
    if " at " in name:
        away_name, home_name = name.split(" at ", 1)
        away = home = None
        for c in comps.values():
            if c.get("name") == away_name.strip():
                away = c.get("abbr")
            if c.get("name") == home_name.strip():
                home = c.get("abbr")
        if away and home:
            return away, home
    away = home = None
    for c in comps.values():
        if c.get("seq") == 1:
            away = c.get("abbr")
        elif c.get("seq") == 0:
            home = c.get("abbr")
    return away, home


def prophetx_ticks(now_utc, now_bj):
    """ProphetX moneyline ticks in the production shadow-row format."""
    evs, pages, err = prophetx_events()
    ticks, tick_err = [], 0
    seen = {}
    for ev in evs:
        try:
            sched = datetime.fromisoformat(
                ev["scheduled"].replace("Z", "+00:00"))
            if sched > now_utc + timedelta(hours=26):
                continue
            away, home = prophetx_sides(ev)
            if not away or not home:
                tick_err += 1
                continue
            gid = gid_of(away, home)
            ml, e = prophetx_moneyline(ev, gid)
            if ml is None:
                tick_err += 1
                continue
            def side(ln):
                best = None
                depth = 0.0
                for od in ln.get("odds", []) or []:
                    try:
                        depth += float(od.get("value") or 0)
                        d = float(od.get("display_odds"))
                    except Exception:  # noqa: BLE001
                        continue
                    if best is None or d > best:
                        best = d
                return {"abbr": ln.get("team_abbr"),
                        "dec": best, "am": None, "depth_usd": round(depth, 2)}
            total = ml["home"].get("total_stake")
            row = {
                "observed_at": now_utc.isoformat().replace("+00:00", "Z"),
                "observed_at_bj": now_bj.isoformat(),
                "event_id": ev.get("event_id"),
                "gid": gid,
                "scheduled": ev.get("scheduled"),
                "moneyline": {"away": side(ml["away"]),
                              "home": side(ml["home"]),
                              "totalStake": total},
                "spread_totalStake": None,
                "total_totalStake": None,
                "source": "backup",
            }
            if gid in seen and (seen[gid].get("moneyline", {}).get("totalStake") or 0) >= (total or 0):
                continue
            seen[gid] = row
            ticks.append(row)
        except Exception:  # noqa: BLE001
            tick_err += 1
    return ticks, {"events": len(evs), "pages": pages, "err": err,
                   "tick_err": tick_err}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="backup_config.json")
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--out", default="gap_snapshot.json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    cfg = {}
    try:
        with open(args.config) as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001
        cfg = {}
    stale_min = float(cfg.get("stale_minutes", 10))
    forced = bool(args.force or cfg.get("force_collect")
                  or os.environ.get("BACKUP_FORCE_COLLECT") == "true")
    enabled = cfg.get("enabled", True)

    now_utc = datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BJ)

    # kill switch: one flag disables collection instantly
    if not enabled:
        summary = {"decision": "disabled", "reason": "kill_switch_enabled_false",
                   "checked_at_bj": now_bj.isoformat()}
        with open("collect_summary.json", "w") as f:
            json.dump(summary, f, ensure_ascii=False)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # freshness gate: read the board's generated_at_bj
    age = None
    try:
        with open(os.path.join(args.repo_dir, "beta_latest.json")) as f:
            gen = json.load(f).get("generated_at_bj")
        if gen:
            age = (now_bj - datetime.fromisoformat(gen)).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        age = None

    if not forced and (age is None or age < stale_min):
        summary = {"decision": "skip", "reason": "board_fresh",
                   "board_age_min": age, "stale_minutes": stale_min,
                   "checked_at_bj": now_bj.isoformat()}
        with open("collect_summary.json", "w") as f:
            json.dump(summary, f, ensure_ascii=False)
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # board is stale (or forced): collect one snapshot of the 4 legs
    run_id = os.environ.get("GITHUB_RUN_ID") or ("local-%d" % int(time.time()))
    gap_start_bj = None
    try:
        with open(os.path.join(args.repo_dir, "beta_latest.json")) as f:
            gap_start_bj = json.load(f).get("generated_at_bj")
    except Exception:  # noqa: BLE001
        pass

    games, err = espn_games(now_bj)
    window = [g for g in games
              if g["status"] in ("pre", "")
              and timedelta(0) <= (g["tipoff_bj"] - now_bj) <= timedelta(hours=24)]
    legs = {"espn": {"games_in_window": len(window), "err": err},
            "kalshi": {"games_ok": 0, "err": None},
            "polymarket": {"games_ok": 0, "err": None},
            "prophetx": {"ticks": 0, "err": None}}

    games_out = {}
    for g in window:
        gid = g["game_id"]
        k, k_err = kalshi_anchor(g)
        p, p_err = poly_anchor(g)
        espn_odds = espn_close_odds(g)
        if k is not None:
            legs["kalshi"]["games_ok"] += 1
        elif k_err:
            legs["kalshi"]["err"] = k_err
        if p is not None:
            legs["polymarket"]["games_ok"] += 1
        elif p_err:
            legs["polymarket"]["err"] = p_err
        tick = {
            "tick_id": "dense-%s-%s" % (gid, now_utc.strftime("%Y%m%dT%H%M%S")),
            "observed_at": now_utc.isoformat().replace("+00:00", "Z"),
            "observed_at_bj": now_bj.isoformat(),
            "minutes_to_tipoff": round(
                (g["tipoff_bj"] - now_bj).total_seconds() / 60.0, 1),
            "interval_min": 5,
            "source": "backup",
            "exchange_anchor": {"kalshi": k, "polymarket": p},
            "odds_action_network": None,
            "odds_an_web": None,
            "odds_espn": ({"status": "ok", "moneyline": espn_odds,
                           "provider": espn_odds["provider"]}
                          if espn_odds else None),
            "odds_oddsportal": None,
            "odds_api": None,
            "betfair": None,
        }
        games_out[gid] = {"dense_tick": tick}

    px_ticks, px_info = prophetx_ticks(now_utc, now_bj)
    legs["prophetx"] = {"ticks": len(px_ticks), "err": px_info.get("err"),
                        "tick_err": px_info.get("tick_err")}

    snapshot = {
        "schema": "nba-backup-gap-snapshot/1",
        "run_id": str(run_id),
        "collected_at_utc": now_utc.isoformat().replace("+00:00", "Z"),
        "collected_at_bj": now_bj.isoformat(),
        "gap_start_bj": gap_start_bj,
        "board_age_min_at_collect": age,
        "forced": forced,
        "legs": legs,
        "games": games_out,
        "prophetx_ticks": px_ticks,
    }
    if not args.dry_run:
        with open(args.out, "w") as f:
            json.dump(snapshot, f, ensure_ascii=False)
    summary = {"decision": "collect",
               "run_id": str(run_id),
               "board_age_min": age, "forced": forced,
               "games": len(games_out),
               "prophetx_ticks": len(px_ticks),
               "legs": {k: (v.get("games_ok", v.get("ticks",
                                                   v.get("games_in_window"))))
                        for k, v in legs.items()},
               "snapshot_bytes": (os.path.getsize(args.out)
                                  if not args.dry_run and os.path.exists(args.out)
                                  else None)}
    with open("collect_summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
