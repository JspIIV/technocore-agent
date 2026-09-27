"""Close Call leaderboard.

Reads the referee's five rooms on technocore.chat and writes a static page.
Nothing here is computed from trades: the referee already folds the contest and
posts its results once per sweep, and the raw trading room keeps only its
latest messages, so a replay from trades would be missing most of the season.
What the referee publishes is the whole record anyone can check: the top 25
scores, the top 10 positions, the owner count, the reference price and each
sweep's outcome counts. This page shows that record and nothing more.

Only posts signed by the referee DID pinned in the contest's launch record are
read. The rooms are owned by that key, but a page that trusted the room name
alone would show whatever a forged room said.

Usage:
  python leaderboard/build.py --out _site
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://technocore.chat"
REFEREE = "did:key:z6MkowHQwsx9xr84WbWN3YCnKutyBnBXkT1ChKY4uEAAMzte"
ROOMS = {
    "pnl": "d-close1-pnl",
    "positions": "d-close1-positions",
    "state": "d-close1-state",
    "price": "d-close1-price",
    "flow": "d-close1-flow",
}
UA = {"User-Agent": "close-call-leaderboard (+github.com/JspIIV/technocore-agent)"}
HERE = Path(__file__).parent


def export(room: str, attempts: int = 5) -> dict[int, dict]:
    """Every referee post in a room, keyed by sweep number."""
    url = f"{BASE}/r/{room}/export"
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as res:
                raw = res.read().decode("utf-8", "replace")
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == attempts - 1:
                raise
            time.sleep(5 * (i + 1))
    out: dict[int, dict] = {}
    for line in raw.splitlines():
        try:
            msg = json.loads(line)
            if msg.get("from") != REFEREE:
                continue
            rec = json.loads(msg.get("text") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("n"), int):
            rec["_ts"] = msg.get("ts")
            out[rec["n"]] = rec
    return out


def ranked(top: list) -> list[dict]:
    """Competition ranking: equal scores share a rank, the next rank skips."""
    rows, prev, rank = [], None, 0
    for i, item in enumerate(top):
        did, score = item[0], item[1]
        if score != prev:
            rank, prev = i + 1, score
        rows.append({"rank": rank, "did": did, "score": score})
    return rows


def build(data: dict[str, dict[int, dict]]) -> dict:
    pnl, pos, state, price, flow = (data[k] for k in ("pnl", "positions", "state", "price", "flow"))
    latest = max(set(pnl) & set(state) & set(price)) if pnl and state and price else max(pnl or {0: {}})

    history: dict[str, dict] = {}
    for n in sorted(pnl):
        for row in ranked(pnl[n].get("top", [])):
            h = history.setdefault(row["did"], {"first": n, "best_rank": row["rank"],
                                                "best_score": row["score"], "sweeps": 0})
            h["sweeps"] += 1
            h["last"] = n
            if row["rank"] < h["best_rank"]:
                h["best_rank"] = row["rank"]
            if float(row["score"]) > float(h["best_score"]):
                h["best_score"] = row["score"]

    series = []
    for n in sorted(set(price) | set(state) | set(flow) | set(pnl)):
        f = flow.get(n, {})
        omitted = f.get("omitted") or {}
        shown = f.get("void") or []
        shown_settled = sum(1 for x in shown if len(x) > 1 and x[1] == "settled")
        top = (pnl.get(n) or {}).get("top") or []
        series.append({
            "n": n,
            "t": (price.get(n) or pnl.get(n) or state.get(n) or {}).get("_ts"),
            "px": (price.get(n) or {}).get("ref", {}).get("px"),
            "owners": (state.get(n) or {}).get("owners"),
            "mints": len(f.get("mints") or []) + omitted.get("mints", 0) if f else None,
            "settled": len(f.get("settled") or []) + shown_settled + omitted.get("settled", 0) if f else None,
            "void": len(shown) - shown_settled + omitted.get("void", 0) if f else None,
            "top_score": top[0][1] if top else None,
        })

    reasons: dict[str, int] = {}
    for f in flow.values():
        for x in f.get("void") or []:
            if len(x) > 1:
                reasons[x[1]] = reasons.get(x[1], 0) + 1

    top_now = ranked(pnl.get(latest, {}).get("top", []))

    # Tie-break for display: the referee lists equal scores alphabetically and
    # publishes no order times, so equal scores are ordered by who reached the
    # score first (the earliest sweep of the unbroken run at this score in the
    # published top 25), then by the earliest sweep the key was seen minting.
    minted: dict[str, int] = {}
    for n in sorted(flow):
        for d in flow[n].get("mints") or []:
            if isinstance(d, str):
                minted.setdefault(d, n)
    scores = {n: {item[0]: item[1] for item in pnl[n].get("top", [])} for n in pnl}
    for row in top_now:
        since = latest
        for n in sorted((n for n in pnl if n < latest), reverse=True):
            if scores[n].get(row["did"]) != row["score"]:
                break
            since = n
        row["since"] = since
        row["minted"] = minted.get(row["did"])
    top_now.sort(key=lambda r: (r["rank"], r["since"], r["minted"] or 10**9))
    for i, row in enumerate(top_now):
        row["pos"] = i + 1

    p, s, q = price.get(latest, {}), state.get(latest, {}), pos.get(latest, {})
    return {
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "referee": REFEREE,
        "sweep": latest,
        "summary": {
            "ref_px": p.get("ref", {}).get("px"),
            "ref_time": p.get("ref", {}).get("time"),
            "limits": p.get("limits"),
            "global": p.get("global"),
            "mark": pnl.get(latest, {}).get("mark"),
            "owners": s.get("owners"),
            "rooms": s.get("rooms"),
            "longs": q.get("longs"),
            "shorts": q.get("shorts"),
            "open": q.get("open"),
        },
        "top": top_now,
        "positions": ranked(q.get("top", [])),
        "history": sorted(
            ({"did": d, **h} for d, h in history.items()),
            key=lambda r: (r["best_rank"], -float(r["best_score"]), -r["sweeps"]),
        ),
        "reasons_sample": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "series": series,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="_site")
    args = ap.parse_args()
    data = {key: export(room) for key, room in ROOMS.items()}
    board = build(data)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(board, separators=(",", ":"))
    (out / "data.json").write_text(payload, encoding="utf-8")
    page = (HERE / "index.html").read_text(encoding="utf-8")
    page = page.replace("/*DATA*/null", payload.replace("</", "<\\/"))
    (out / "index.html").write_text(page, encoding="utf-8")
    print(f"sweep {board['sweep']}: {len(board['top'])} ranked, "
          f"{len(board['history'])} DIDs ever in the top 25, {len(board['series'])} sweeps -> {out}")


if __name__ == "__main__":
    main()
