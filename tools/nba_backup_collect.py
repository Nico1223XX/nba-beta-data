#!/usr/bin/env python3
"""NBA backup collector (GitHub Actions runner side).

Runs every 5 min from the board repo workflow. FIRST checks whether the
local VM is alive (beta_latest.json generated_at_bj freshness); if the
board is fresh, exits immediately WITHOUT collecting. Only when the board
is stale (> stale_minutes, default 8) -- i.e. the VM is dead -- does it
collect one snapshot of the 5 legs and upload it as a GitHub Actions
artifact (retention 1 day). No data is ever committed to git.

Legs (same fetch logic / conventions as the production collectors):
  espn       - ESPN scoreboard API, DraftKings close moneyline (company line)
  kalshi     - Kalshi markets API per game (mid/bid/ask/last/OI/volume)
  polymarket - Polymarket Gamma events API per game (outcome prices)
  prophetx   - ProphetX events + markets API (moneyline dec/am/depth/stake)
  dknet      - DK Network splits page via Playwright + system Chrome
  (AN is skipped: blocked from runner IPs. ESPN covers the company line.)

Output: gap_snapshot.json in the working dir + a JSON summary on stdout.
Always exits 0 (the verdict is in the output, not the exit code).
Stdlib only, except Playwright for the DKNet leg.
Usage: python3 nba_backup_collect.py [--config PATH] [--out PATH]
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 25
ET = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
ESPN_ABBR = {"WSH": "WAS", "SA": "SAS", "NO": "NOP",
             "GS": "GSW", "UTAH": "UTA", "NY": "NYK"}
CITY_CODE = {"ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
             "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
             "DET": "DET", "GS": "GSW", "GSW": "GSW", "HOU": "HOU",
             "IND": "IND", "LAC": "LAC", "LAL": "LAL", "MEM": "MEM",
             "MIA": "MIA", "MIL": "MIL", "MIN": "MIN", "NO": "NOP",
             "NOP": "NOP", "NY": "NYK", "NYK": "NYK", "OKC": "OKC",
             "ORL": "ORL", "PHI": "PHI", "PHX": "PHX", "POR": "POR",
             "SAC": "SAC", "SA": "SAS", "SAS": "SAS", "TOR": "TOR",
             "UTA": "UTA", "WAS": "WAS"}
NICK_CODE = {"Hawks": "ATL", "Celtics": "BOS", "Nets": "BKN",
             "Hornets": "CHA", "Bulls": "CHI", "Cavaliers": "CLE",
             "Mavericks": "DAL", "Nuggets": "DEN", "Pistons": "DET",
             "Warriors": "GSW", "Rockets": "HOU", "Pacers": "IND",
             "Clippers": "LAC", "Lakers": "LAL", "Grizzlies": "MEM",
             "Heat": "MIA", "Bucks": "MIL", "Timberwolves": "MIN",
             "Pelicans": "NOP", "Knicks": "NYK", "Thunder": "OKC",
             "Magic": "ORL", "76ers": "PHI", "Suns": "PHX",
             "Trail Blazers": "POR", "Kings": "SAC", "Spurs": "SAS",
             "Raptors": "TOR", "Jazz": "UTA", "Wizards": "WAS"}
KALSHI_MARKETS = ("https://api.elections.kalshi.com/trade-api/v2/markets"
                  "?event_ticker={}&limit=10")
POLY_EVENT = "https://gamma-api.polymarket.com/events?slug={}"
PX_BASE = "https://cash.api.prophetx.co/trade/public/api/v1"
DKNET_URL = ("https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
             "?tb_eg=42648&itm_content=NBA&tb_edate=n7days")


def fetch(url, attempts=2):
    """GET url; returns (body_text_or_None, http_status_or_None, err_or_None)."""
    last_err = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", errors="ignore"), resp.status, None
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            try:
                e.read()
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, str(e)[:100])
        time.sleep(2)
    return None, None, last_err


def fetch_json(url, attempts=2):
    body, status, err = fetch(url, attempts=attempts)
    if body is None:
        return None, err or ("http_%s" % status)
    try:
        return json.loads(body), None
    except Exception as e:  # noqa: BLE001
        return None, "json %s" % str(e)[:60]


def am_to_dec(o):
    if o is None:
        return None
    try:
        o = float(o)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    return round(1 + o / 100, 3) if o > 0 else round(1 + 100 / abs(o), 3)


def load_config(path):
    cfg = {"stale_minutes": 8, "force_collect": False}
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def board_age_minutes(repo_dir):
    """Minutes since beta_latest.json was generated; None if unreadable."""
    try:
        with open(os.path.join(repo_dir, "beta_latest.json")) as f:
            d = json.load(f)
        gen = d.get("generated_at_bj")
        if not gen:
            return None
        t = datetime.fromisoformat(gen)
        now = datetime.now(timezone.utc)
        return (now - t).total_seconds() / 60.0
    except Exception:
        return None


def espn_schedule():
    """Games in the next 24h from ESPN scoreboard (US today + tomorrow).

    Returns {game_id: {away, home, tip_utc, date_us, espn_odds}}.
    espn_odds is the production match_espn dict (close odds, may be None).
    """
    now_us = datetime.now(timezone.utc).astimezone(ET)
    dates = [(now_us + timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1)]
    games = {}
    errs = []
    for ds in dates:
        url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
               "scoreboard?dates=%s" % ds)
        d, err = fetch_json(url)
        if d is None:
            errs.append("%s: %s" % (ds, err))
            continue
        for ev in d.get("events") or []:
            try:
                comp = (ev.get("competitions") or [{}])[0]
                teams = {}
                for c in comp.get("competitors", []):
                    teams[c.get("homeAway")] = c.get("team", {}).get("abbreviation")
                away = ESPN_ABBR.get(teams.get("away"), teams.get("away"))
                home = ESPN_ABBR.get(teams.get("home"), teams.get("home"))
                tip = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
                if not away or not home:
                    continue
                us_date = tip.astimezone(ET).strftime("%Y%m%d")
                gid = "%s-%s-%s" % (us_date, away, home)
                odds = (comp.get("odds") or [{}])[0]
                ml = odds.get("moneyline") or {}
                hc = ((ml.get("home") or {}).get("close") or {}).get("odds")
                ac = ((ml.get("away") or {}).get("close") or {}).get("odds")
                games[gid] = {
                    "away": away, "home": home, "tip_utc": tip,
                    "date_us": us_date,
                    "espn_odds": {
                        "provider": ((odds.get("provider") or {}).get("name")),
                        "spread_home": odds.get("spread"),
                        "total": odds.get("overUnder"),
                        "details": odds.get("details"),
                        "ml_home_decimal": am_to_dec(hc),
                        "ml_away_decimal": am_to_dec(ac),
                    },
                }
            except Exception:
                continue
    now = datetime.now(timezone.utc)
    out = {}
    for gid, g in games.items():
        mins = (g["tip_utc"] - now).total_seconds() / 60.0
        if 0 < mins <= 24 * 60:
            g["minutes_to_tipoff"] = round(mins, 1)
            out[gid] = g
    return out, errs


def kalshi_anchor(gid):
    date, away, home = gid.split("-")
    ev = "KXNBAGAME-%s%s%s%s%s" % (date[2:4], MON[int(date[4:6]) - 1],
                                   date[6:8], away, home)
    d, err = fetch_json(KALSHI_MARKETS.format(ev))
    if d is None:
        return None, err
    out = {}
    for m in d.get("markets", []) or []:
        try:
            code = m["ticker"].rsplit("-", 1)[-1]
            bid = float(m["yes_bid_dollars"])
            ask = float(m["yes_ask_dollars"])
            out[code] = {
                "bid": bid, "ask": ask,
                "mid": round((bid + ask) / 2, 4),
                "last": float(m["last_price_dollars"]),
                "open_interest": float(m["open_interest_fp"]),
                "volume": float(m.get("volume_fp") or 0),
            }
        except Exception:
            continue
    return (out or None), None


def polymarket_anchor(gid):
    date, away, home = gid.split("-")
    slug = "nba-%s-%s-%s-%s-%s" % (away.lower(), home.lower(),
                                   date[:4], date[4:6], date[6:8])
    d, err = fetch_json(POLY_EVENT.format(slug))
    if d is None:
        return None, err
    if not d:
        return None, "empty_event_list"
    try:
        mk = (d[0].get("markets") or [{}])[0]
        names = json.loads(mk.get("outcomes", "[]"))
        prices = json.loads(mk.get("outcomePrices", "[]"))
        return {"prices": dict(zip(names, [float(p) for p in prices])),
                "volume": float(mk.get("volume", 0) or 0)}, None
    except Exception as e:  # noqa: BLE001
        return None, "parse %s" % str(e)[:60]


def prophetx_events():
    """Discover NBA events in the next 24h (production discovery logic)."""
    now = int(time.time())
    deadline = now + 24 * 3600
    cursor = now
    found = []
    pages = 0
    err = None
    while pages < 60:
        d, e = fetch_json("%s/events?limit=100&next=%s" % (PX_BASE, cursor))
        if d is None:
            err = e
            break
        pages += 1
        evs = d.get("data") or []
        if not evs:
            break
        last_ts = None
        for ev in evs:
            try:
                ts = datetime.fromisoformat(
                    (ev.get("scheduled") or "").replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            last_ts = ts
            if ts > deadline:
                continue
            if (ev.get("sport") or {}).get("name") != "Basketball":
                continue
            if (ev.get("tournament") or {}).get("name") != "NBA":
                continue
            comps = {}
            for c in ev.get("competitors", []) or []:
                comps[c.get("id")] = {"abbr": c.get("abbreviation"),
                                     "name": c.get("name"),
                                     "seq": c.get("seq")}
            found.append({"event_id": ev.get("id"), "name": ev.get("name"),
                          "scheduled": ev.get("scheduled"),
                          "scheduled_ts": ts, "competitors": comps})
        if last_ts is not None and last_ts > deadline:
            break
        nxt = d.get("next")
        if not nxt or int(nxt) == cursor:
            break
        cursor = int(nxt)
    seen = {}
    for f in found:
        seen.setdefault(f["event_id"], f)
    return sorted(seen.values(), key=lambda x: x["scheduled_ts"]), pages, err


def _px_team_sides(event):
    name = event.get("name") or ""
    comps = event.get("competitors") or {}
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


def _px_moneyline_sides(market, event):
    comps = event.get("competitors") or {}
    away_abbr, home_abbr = _px_team_sides(event)
    out = {}
    for levels in market.get("selections") or []:
        if not levels:
            continue
        cid = levels[0].get("competitorId")
        abbr = (comps.get(str(cid)) or {}).get("abbr") or \
            (comps.get(cid) or {}).get("abbr")
        if not abbr:
            abbr = (levels[0].get("abbreviatedName") or "").split(" ")[0] or None
        if not abbr:
            continue
        best_dec, best_am, depth = None, None, 0.0
        for lv in levels:
            try:
                o = float(lv.get("odds"))
                dec = 1.0 + o / 100.0 if o > 0 else 1.0 + 100.0 / abs(o)
            except Exception:
                continue
            if best_dec is None or dec > best_dec:
                best_dec, best_am = dec, lv.get("odds")
            try:
                depth += float(lv.get("value") or 0)
            except Exception:
                pass
        out[abbr] = {"dec": round(best_dec, 4) if best_dec else None,
                     "am": best_am, "depth_usd": round(depth, 2)}
    res = {}
    for key, abbr in (("away", away_abbr), ("home", home_abbr)):
        if abbr and abbr in out:
            res[key] = {"abbr": abbr, **out[abbr]}
    for abbr, v in out.items():
        if abbr not in (away_abbr, home_abbr):
            res[abbr] = {"abbr": abbr, **v}
    return res, away_abbr, home_abbr


def prophetx_ticks(now_utc, now_bj):
    """One ProphetX tick per NBA event, production row format + source=backup."""
    events, pages, err = prophetx_events()
    ticks = []
    n_err = 0
    for ev in events:
        if ev["scheduled_ts"] <= now_utc.timestamp():
            continue
        eid = ev.get("event_id")
        d, e = fetch_json("%s/events/%s/markets" % (PX_BASE, eid))
        if d is None:
            n_err += 1
            continue
        markets = (d.get("data") or {}).get("markets", []) or []
        ml = None
        spread = total = None
        for m in markets:
            if m.get("name") == "Moneyline" and m.get("status") == "active":
                ml = m
            elif m.get("name") == "Spread":
                spread = m
            elif m.get("name") == "Total Points":
                total = m
        if not ml:
            n_err += 1
            continue
        sides, away_abbr, home_abbr = _px_moneyline_sides(ml, ev)
        gid = ("%s-%s" % (away_abbr, home_abbr)) if away_abbr and home_abbr else str(eid)
        ticks.append({
            "observed_at": now_utc.isoformat(),
            "observed_at_bj": now_bj.isoformat(),
            "event_id": eid, "gid": gid,
            "scheduled": ev.get("scheduled"),
            "moneyline": {**sides, "totalStake": ml.get("totalStake"),
                          "market_id": ml.get("id")},
            "spread_totalStake": (spread or {}).get("totalStake"),
            "total_totalStake": (total or {}).get("totalStake"),
            "source": "backup",
        })
    # Dedupe by gid: keep the tick with the highest moneyline totalStake
    # (same rule as the production collector: duplicate listings exist).
    if ticks:
        best = {}
        for t in ticks:
            g = t.get("gid")
            st = (t.get("moneyline") or {}).get("totalStake") or 0
            cur = best.get(g)
            if cur is None or st > ((cur.get("moneyline") or {}).get("totalStake") or 0):
                best[g] = t
        ticks = list(best.values())
    return ticks, {"events": len(events), "pages": pages, "err": err, "tick_err": n_err}


def _dknet_code_from_header(name):
    if not name:
        return None
    parts = name.strip().split()
    if not parts:
        return None
    c = CITY_CODE.get(parts[0].upper())
    if c:
        return c
    return NICK_CODE.get(parts[-1])


def _dknet_parse_spread(sel):
    m = re.match(r"^(.*?)\s+([+-]?\d+(?:\.\d+)?)\s*$", (sel or "").strip())
    if m:
        try:
            return m.group(1).strip(), float(m.group(2))
        except ValueError:
            return sel, None
    return sel, None


def _dknet_convert_game(g):
    """DK Network single game -> splits markets dict (production converter)."""
    mk = g.get("markets") or {}
    ml = mk.get("moneyline") or []
    sp = mk.get("spread") or []
    tot = mk.get("total") or []
    if len(ml) != 2 or len(sp) != 2 or len(tot) != 2:
        return None, "market_rows_not_2"
    ml_map = {}
    for r in ml:
        c = _dknet_code_from_header(r.get("selection"))
        if c is None:
            return None, "ml_nick_unknown:%s" % r.get("selection")
        ml_map[c] = {"tickets_pct": r.get("bets_pct"),
                     "money_pct": r.get("handle_pct"), "line": 0}
    sp_map = {}
    for r in sp:
        nick, line = _dknet_parse_spread(r.get("selection"))
        c = _dknet_code_from_header(nick)
        if c is None or line is None:
            return None, "spread_parse_fail:%s" % r.get("selection")
        sp_map[c] = {"tickets_pct": r.get("bets_pct"),
                     "money_pct": r.get("handle_pct"), "line": line}
    tot_map = {}
    for r in tot:
        sel = (r.get("selection") or "").strip()
        if sel.lower().startswith("over"):
            side = "over"
        elif sel.lower().startswith("under"):
            side = "under"
        else:
            return None, "total_side_unknown:%s" % sel
        line = r.get("line")
        if line is None:
            return None, "total_line_missing"
        tot_map[side] = {"tickets_pct": r.get("bets_pct"),
                         "money_pct": r.get("handle_pct"), "line": line}
    for mkt, d in (("moneyline", ml_map), ("spread", sp_map)):
        vals = list(d.values())
        if len(vals) != 2:
            return None, "market_%s_not_2_sides" % mkt
        for v in vals:
            if v["tickets_pct"] is None or v["money_pct"] is None:
                return None, "market_%s_pct_missing" % mkt
            if not (0 <= v["tickets_pct"] <= 100 and 0 <= v["money_pct"] <= 100):
                return None, "market_%s_pct_range" % mkt
        if abs(vals[0]["tickets_pct"] + vals[1]["tickets_pct"] - 100) > 1:
            return None, "market_%s_tickets_sum" % mkt
        if abs(vals[0]["money_pct"] + vals[1]["money_pct"] - 100) > 1:
            return None, "market_%s_money_sum" % mkt
    for side, v in tot_map.items():
        if v["tickets_pct"] is None or v["money_pct"] is None:
            return None, "total_pct_missing"
    return {"moneyline": ml_map, "spread": sp_map, "total": tot_map}, None


def dknet_scrape():
    """DK Network splits via Playwright + system Chrome (runner only).

    Returns (games_list_or_None, error_or_None). Port of the production
    dknet_scrape.js (same URL, same wait condition, same line parser).
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return None, "playwright_import:%s" % str(e)[:80]
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome",
                                            args=["--no-sandbox",
                                                  "--disable-dev-shm-usage"])
            except Exception:
                browser = p.chromium.launch(
                    executable_path="/usr/bin/google-chrome",
                    args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0.0.0 Safari/537.36"))
            page.goto(DKNET_URL, wait_until="commit", timeout=60000)
            try:
                page.wait_for_function(
                    "() => { const t = document.body.innerText; "
                    "return t.includes('Celtics') && t.includes('MONEYLINE') "
                    "&& t.includes('%'); }",
                    timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            text = page.evaluate("() => document.body.innerText")
            browser.close()
    except Exception as e:  # noqa: BLE001
        return None, "playwright:%s" % str(e)[:120]
    lines = [s.strip() for s in text.split("\n")]
    lines = [s for s in lines if s]
    games = []
    cur = None
    cur_mkt = None
    pct = lambda s: (lambda m: float(m.group(1)) if m else None)(
        re.search(r"(\d+(?:\.\d+)?)\s*%", str(s)))
    odds_n = lambda s: (lambda m: int(m.group(1)) if m else None)(
        re.search(r"([+-]?\d{2,4})", str(s).replace("\u2212", "-")))
    line_n = lambda s: (lambda m: float(m.group(1)) if m else None)(
        re.search(r"([+-]?\d+(?:\.\d+)?)\s*$", str(s)))
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        m = re.match(r"^(.+?)\s+@\s+(.+)$", ln)
        if m and len(ln) < 60 and not ln.startswith("http"):
            if cur:
                games.append(cur)
            cur = {"away": m.group(1).strip(), "home": m.group(2).strip(),
                   "game_time": "",
                   "markets": {"moneyline": [], "spread": [], "total": []}}
            cur_mkt = None
            i += 1
            continue
        if not cur:
            i += 1
            continue
        if re.match(r"^(Wed|Thu|Fri|Sat|Sun|Mon|Tue) ", ln) and "ET" in ln:
            cur["game_time"] = ln
            i += 1
            continue
        if re.match(r"^(SELECTION|ODDS|HANDLE|BETS|ADVERTISEMENT)$", ln, re.I):
            i += 1
            continue
        mk = re.match(r"^(MONEYLINE|SPREAD|TOTAL)$", ln, re.I)
        if mk:
            cur_mkt = mk.group(1).lower()
            i += 1
            continue
        if cur_mkt and i + 3 < n:
            sel, odds, handle, bets = lines[i], lines[i + 1], lines[i + 2], lines[i + 3]
            if pct(handle) is not None and pct(bets) is not None and odds_n(odds) is not None:
                cur["markets"][cur_mkt].append({
                    "selection": sel,
                    "line": None if cur_mkt == "moneyline" else line_n(sel),
                    "odds": odds_n(odds),
                    "handle_pct": pct(handle), "bets_pct": pct(bets)})
                i += 4
                continue
        i += 1
    if cur:
        games.append(cur)
    return games, None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="backup_config.json")
    ap.add_argument("--out", default="gap_snapshot.json")
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + assemble, print summary, do not write snapshot")
    args = ap.parse_args()

    now_utc = datetime.now(timezone.utc)
    now_bj = now_utc.astimezone(BJ)
    cfg = load_config(args.config)
    age = board_age_minutes(args.repo_dir)
    forced = bool(cfg.get("force_collect"))
    stale_min = float(cfg.get("stale_minutes") or 8)
    if not forced and (age is None or age < stale_min):
        summary = {"decision": "skip", "reason": "board_fresh",
                   "board_age_min": age, "stale_minutes": stale_min,
                   "checked_at_bj": now_bj.isoformat()}
        try:
            with open("collect_summary.json", "w") as f:
                json.dump(summary, f, ensure_ascii=False)
        except Exception:
            pass
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    run_id = os.environ.get("GITHUB_RUN_ID") or ("local-%d" % int(time.time()))
    gap_start_bj = None
    try:
        with open(os.path.join(args.repo_dir, "beta_latest.json")) as f:
            gap_start_bj = json.load(f).get("generated_at_bj")
    except Exception:
        pass

    legs = {}
    schedule, espn_errs = espn_schedule()
    legs["espn"] = {"games_in_window": len(schedule), "errors": espn_errs}

    games_out = {}
    kalshi_ok = poly_ok = 0
    kalshi_errs, poly_errs = [], []
    for gid, g in sorted(schedule.items()):
        anchor = {"kalshi": None, "polymarket": None}
        k, k_err = kalshi_anchor(gid)
        if k is not None:
            anchor["kalshi"] = k
            kalshi_ok += 1
        elif k_err:
            kalshi_errs.append("%s: %s" % (gid, k_err))
        pp, p_err = polymarket_anchor(gid)
        if pp is not None:
            anchor["polymarket"] = pp
            poly_ok += 1
        elif p_err:
            poly_errs.append("%s: %s" % (gid, p_err))
        tick = {
            "tick_id": "dense-backup-%s-%s" % (gid, now_bj.strftime("%Y%m%dT%H%M%S")),
            "observed_at": now_utc.isoformat(),
            "observed_at_bj": now_bj.isoformat(),
            "minutes_to_tipoff": g["minutes_to_tipoff"],
            "interval_min": 5,
            "source": "backup",
            "exchange_anchor": anchor,
            "odds_action_network": {"status": "null",
                                    "reason": "backup_no_an_runner_blocked"},
            "odds_an_web": {"status": "null",
                            "reason": "backup_no_an_runner_blocked"},
            "odds_espn": {"status": "ok", **g["espn_odds"]},
            "odds_oddsportal": {"status": "null",
                                "reason": "js_rendered_not_scriptable"},
            "odds_api": {"status": "null",
                         "reason": "odds_api_disabled_per_20261114"},
            "betfair": {"status": "null",
                        "reason": "brazil_egress_blocked_expected_null_no_attempt"},
        }
        games_out[gid] = {"dense_tick": tick, "dknet_tick": None,
                          "away": g["away"], "home": g["home"],
                          "tip_utc": g["tip_utc"].isoformat(),
                          "date_us": g["date_us"]}
    legs["kalshi"] = {"games_ok": kalshi_ok, "errors": kalshi_errs}
    legs["polymarket"] = {"games_ok": poly_ok, "errors": poly_errs}

    px_ticks, px_info = prophetx_ticks(now_utc, now_bj)
    legs["prophetx"] = {"ticks": len(px_ticks), "events": px_info["events"],
                        "pages": px_info["pages"], "err": px_info["err"],
                        "tick_err": px_info["tick_err"]}

    dk_games, dk_err = dknet_scrape()
    dk_ok = dk_conv_err = 0
    dk_errs = []
    if dk_games is None:
        legs["dknet"] = {"games_ok": 0, "error": dk_err}
    else:
        for g in dk_games:
            away = _dknet_code_from_header(g.get("away"))
            home = _dknet_code_from_header(g.get("home"))
            if not away or not home:
                dk_errs.append("header_unknown:%s @ %s" % (g.get("away"), g.get("home")))
                continue
            # match by team pair to the schedule game nearest tipoff in window
            best_gid, best_mins = None, None
            for gid, gg in schedule.items():
                if gg["away"] != away or gg["home"] != home:
                    continue
                mins = gg["minutes_to_tipoff"]
                if best_mins is None or abs(mins) < abs(best_mins):
                    best_gid, best_mins = gid, mins
            if not best_gid:
                dk_errs.append("no_match_or_out_of_window:%s-%s" % (away, home))
                continue
            markets, conv_err = _dknet_convert_game(g)
            if markets is None:
                dk_errs.append("convert_fail:%s:%s" % (best_gid, conv_err))
                dk_conv_err += 1
                continue
            gg = schedule[best_gid]
            tick = {
                "tick_id": "splits-backup-%s-%s" % (best_gid, now_bj.strftime("%Y%m%dT%H%M%S")),
                "observed_at": now_utc.isoformat(),
                "observed_at_bj": now_bj.isoformat(),
                "minutes_to_tipoff": best_mins,
                "interval_min": 5,
                "source": "DK Network",
                "markets": {
                    "spread": {"home": markets["spread"].get(gg["home"]),
                               "away": markets["spread"].get(gg["away"])},
                    "total": markets["total"],
                    "moneyline": {"home": markets["moneyline"].get(gg["home"]),
                                  "away": markets["moneyline"].get(gg["away"])},
                },
            }
            if tick["markets"]["spread"]["home"] is None or \
               tick["markets"]["spread"]["away"] is None:
                dk_errs.append("spread_side_missing:%s" % best_gid)
                continue
            if tick["markets"]["moneyline"]["home"] is None or \
               tick["markets"]["moneyline"]["away"] is None:
                dk_errs.append("ml_side_missing:%s" % best_gid)
                continue
            games_out[best_gid]["dknet_tick"] = tick
            dk_ok += 1
        legs["dknet"] = {"games_ok": dk_ok, "convert_fail": dk_conv_err,
                         "errors": dk_errs}

    snapshot = {
        "schema": "nba-backup-gap-snapshot/1",
        "run_id": str(run_id),
        "collected_at_utc": now_utc.isoformat(),
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
               "dknet_games": dk_ok,
               "prophetx_ticks": len(px_ticks),
               "legs": {k: (v.get("games_ok", v.get("ticks", v.get("games_in_window"))))
                        for k, v in legs.items()},
               "snapshot_bytes": (os.path.getsize(args.out)
                                  if not args.dry_run and os.path.exists(args.out)
                                  else None)}
    try:
        with open("collect_summary.json", "w") as f:
            json.dump(summary, f, ensure_ascii=False)
    except Exception:
        pass
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
