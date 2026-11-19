#!/usr/bin/env python3
"""Batch connectivity probe (2026-11-19, for GitHub Actions runner test).

AN webpage leg is already proven BLOCKED from a GitHub runner
(2026-11-19: 3 fetches all returned a page with no __NEXT_DATA__).
This probe tests the OTHER five legs in one run, reusing the exact
fetch logic of the production collectors (same URLs, same parsing):
  espn       - ESPN scoreboard API (company-line backup leg)
  kalshi     - Kalshi events API, KXNBAGAME series (odds line)
  polymarket - Polymarket Gamma markets API (odds line)
  prophetx   - ProphetX events API, NBA filter (odds line)
  dknet      - DK Network splits page (DK money backup leg; JS-rendered,
               plain HTTP can only prove reachability, full collection
               needs Playwright -- the probe reports both facts)
  an_control - AN public-betting webpage (expected FAIL on a runner;
               proves the probe is honest / same IP class as the AN test)

Stdlib only (urllib). No keys, no cookies, no writes. Prints one JSON
object per leg plus a summary line; always exits 0 (the verdict is in
the output, not the exit code) so the workflow always commits it.
Usage: python3 batch_connectivity_probe.py
"""
import json
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


def probe_an_control():
    url = "https://www.actionnetwork.com/nba/public-betting"
    for _ in range(2):
        body, status, err = fetch(url, attempts=1)
        if body is not None and "__NEXT_DATA__" in body:
            return True, {"http_status": status, "has_next_data": True,
                          "bytes": len(body)}
        time.sleep(2)
    return False, {"http_status": status, "error": err,
                   "has_next_data": False}


def probe_espn():
    now_us = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    dates = [(now_us + timedelta(days=d)).strftime("%Y%m%d") for d in (0, 1)]
    n_events = 0
    n_with_odds = 0
    providers = set()
    errs = []
    for ds in dates:
        url = ("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
               "scoreboard?dates=%s" % ds)
        body, status, err = fetch(url)
        if body is None:
            errs.append("%s: %s" % (ds, err))
            continue
        try:
            d = json.loads(body)
        except Exception as e:  # noqa: BLE001
            errs.append("%s: json %s" % (ds, str(e)[:60]))
            continue
        for ev in d.get("events") or []:
            n_events += 1
            try:
                comp = (ev.get("competitions") or [{}])[0]
                odds = (comp.get("odds") or [{}])[0]
                ml = odds.get("moneyline") or {}
                hc = ((ml.get("home") or {}).get("close") or {}).get("odds")
                ac = ((ml.get("away") or {}).get("close") or {}).get("odds")
                if hc is not None or ac is not None:
                    n_with_odds += 1
                    prov = ((odds.get("provider") or {}).get("name"))
                    if prov:
                        providers.add(prov)
            except Exception:  # noqa: BLE001
                continue
    ok = n_with_odds >= 1
    return ok, {"events": n_events, "with_odds": n_with_odds,
                "providers": sorted(providers), "errors": errs}


def probe_kalshi():
    url = ("https://api.elections.kalshi.com/trade-api/v2/events"
           "?status=open&series_ticker=KXNBAGAME&with_nested_markets=true&limit=50")
    body, status, err = fetch(url)
    if body is None:
        return False, {"http_status": status, "error": err}
    try:
        d = json.loads(body)
    except Exception as e:  # noqa: BLE001
        return False, {"http_status": status, "error": "json %s" % str(e)[:60]}
    evs = d.get("events") or []
    return len(evs) > 0, {"http_status": status, "events": len(evs)}


def probe_polymarket():
    url = "https://gamma-api.polymarket.com/markets?limit=3&active=true"
    body, status, err = fetch(url)
    if body is None:
        return False, {"http_status": status, "error": err}
    try:
        d = json.loads(body)
    except Exception as e:  # noqa: BLE001
        return False, {"http_status": status, "error": "json %s" % str(e)[:60]}
    if not isinstance(d, list):
        return False, {"http_status": status, "error": "unexpected payload"}
    return len(d) > 0, {"http_status": status, "markets": len(d)}


def probe_prophetx():
    # Mirror production discovery (prophetx_shadow_collect.py): walk
    # forward from now, up to 24h out, up to 60 pages of 100.
    import time as _time
    base = "https://cash.api.prophetx.co/trade/public/api/v1"
    now = int(_time.time())
    deadline = now + 24 * 3600
    cursor = now
    n_nba = 0
    pages = 0
    err = None
    while pages < 60:
        url = base + "/events?limit=100&next=%s" % cursor
        body, status, e = fetch(url)
        if body is None:
            err = e
            break
        try:
            d = json.loads(body)
        except Exception as ex:  # noqa: BLE001
            err = "json %s" % str(ex)[:60]
            break
        pages += 1
        evs = d.get("data") or []
        if not evs:
            break
        last_ts = None
        for ev in evs:
            try:
                ts = datetime.fromisoformat(
                    (ev.get("scheduled") or "").replace("Z", "+00:00")
                ).timestamp()
            except Exception:  # noqa: BLE001
                continue
            last_ts = ts
            if ts > deadline:
                continue
            if (ev.get("sport") or {}).get("name") != "Basketball":
                continue
            if (ev.get("tournament") or {}).get("name") != "NBA":
                continue
            n_nba += 1
        if last_ts is not None and last_ts > deadline:
            break
        nxt = d.get("next")
        if not nxt or int(nxt) == cursor:
            break
        cursor = int(nxt)
    return n_nba > 0, {"nba_events": n_nba, "pages": pages, "error": err}


def probe_dknet():
    url = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
    body, status, err = fetch(url)
    if body is None:
        return False, {"http_status": status, "error": err,
                       "reachable": False, "data_in_html": False}
    markers = {m: (m in body) for m in
               ("Betting Splits", "Handle%", "Bets%", "__NEXT_DATA__")}
    # Data markers are the numbers themselves (Handle%/Bets%); the page
    # title alone does not prove the splits are in the HTML.
    data_in_html = markers["Handle%"] or markers["Bets%"]
    # Reachability is the pass criterion for this leg; data extraction
    # needs Playwright (JS-rendered page) -- reported separately.
    return True, {"http_status": status, "bytes": len(body),
                  "reachable": True, "data_in_html": data_in_html,
                  "markers": markers,
                  "note": "plain HTTP proves reachability only; "
                          "splits data needs Playwright"}


PROBES = [
    ("an_control", probe_an_control),
    ("espn", probe_espn),
    ("kalshi", probe_kalshi),
    ("polymarket", probe_polymarket),
    ("prophetx", probe_prophetx),
    ("dknet", probe_dknet),
]


def main():
    results = {}
    for name, fn in PROBES:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, {"error": "%s: %s" % (type(e).__name__, str(e)[:100])}
        results[name] = {"ok": bool(ok), "detail": detail}
        print(json.dumps({"leg": name, "ok": bool(ok), "detail": detail},
                         ensure_ascii=False))
    passed = [n for n, r in results.items() if r["ok"]]
    failed = [n for n, r in results.items() if not r["ok"]]
    print(json.dumps({"summary": {"passed": passed, "failed": failed}},
                   ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
