#!/usr/bin/env python3
"""ProphetX focused retest (2026-11-19, for GitHub Actions runner).

The batch probe found 0 NBA events in 3 pages on the runner, while the
same probe found 4 locally at 17:52 and 1 locally at 18:57 -- the count
is sliding, which points at pagination/timing, not a block. This probe
diagnoses it: it walks the FULL pagination (up to 60 pages) without the
24h deadline break, logs the scheduled-time range of every page, and
reports exactly where the NBA events sit (and whether the 4 known
tomorrow-morning games are in the list at all).

Stdlib only (urllib). No keys, no cookies, no writes. Always exits 0.
Usage: python3 prophetx_retest.py
"""
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

BASE = "https://cash.api.prophetx.co/trade/public/api/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
TIMEOUT = 25
# The 4 known NBA games for tomorrow morning (US 11-19), by abbreviation.
WANT = {frozenset(("GSW", "MEM")), frozenset(("LAC", "SAS")),
        frozenset(("NYK", "MIN")), frozenset(("POR", "MIL"))}


def fetch(url, attempts=2):
    last_err = None
    for _ in range(attempts):
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
            last_err = "%s: %s" % (type(e).__name__, str(e)[:80])
    return None, None, last_err


def main():
    now = int(time.time())
    print(json.dumps({"runner_now_utc": datetime.now(timezone.utc).isoformat(),
                      "runner_now_ts": now}))
    cursor = now
    pages = []
    nba = []
    found_want = []
    stop_reason = None
    total_events = 0
    for p in range(1, 61):
        url = BASE + "/events?limit=100&next=%s" % cursor
        body, status, err = fetch(url)
        if body is None:
            stop_reason = "fetch_error:%s" % err
            break
        try:
            d = json.loads(body)
        except Exception as ex:  # noqa: BLE001
            stop_reason = "json:%s" % str(ex)[:60]
            break
        evs = d.get("data") or []
        total_events += len(evs)
        if not evs:
            stop_reason = "empty_page"
            break
        times = []
        n_bball = 0
        page_nba = []
        for ev in evs:
            try:
                ts = datetime.fromisoformat(
                    (ev.get("scheduled") or "").replace("Z", "+00:00")
                ).timestamp()
            except Exception:  # noqa: BLE001
                continue
            times.append(ts)
            if (ev.get("sport") or {}).get("name") != "Basketball":
                continue
            n_bball += 1
            if (ev.get("tournament") or {}).get("name") != "NBA":
                continue
            abbrs = [c.get("abbreviation") for c in
                     (ev.get("competitors") or []) if c.get("abbreviation")]
            rec = {"page": p, "name": ev.get("name"),
                   "scheduled": ev.get("scheduled"), "abbrs": abbrs}
            page_nba.append(rec)
            nba.append(rec)
            key = frozenset(abbrs)
            if key in WANT and key not in [frozenset(f["abbrs"]) for f in found_want]:
                found_want.append(rec)
        pages.append({
            "page": p, "events": len(evs),
            "first_scheduled": datetime.fromtimestamp(min(times), timezone.utc).isoformat() if times else None,
            "last_scheduled": datetime.fromtimestamp(max(times), timezone.utc).isoformat() if times else None,
            "basketball": n_bball, "nba": len(page_nba),
        })
        nxt = d.get("next")
        if not nxt or int(nxt) == cursor:
            stop_reason = "cursor_stuck_or_none"
            break
        cursor = int(nxt)
    else:
        stop_reason = "page_cap_60"
    print(json.dumps({
        "stop_reason": stop_reason,
        "pages_walked": len(pages),
        "total_events_seen": total_events,
        "nba_events": len(nba),
        "nba_list": nba,
        "want_found": len(found_want),
        "want_list": found_want,
        "pages": pages,
    }))
    print("exit=0")


if __name__ == "__main__":
    main()
