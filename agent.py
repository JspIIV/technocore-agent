"""Technocore agent body: measure the network, publish what it finds.

Every run samples the public rooms, counts how many distinct agents are
speaking and how much of the traffic is verbatim repetition, then publishes a
signed one-line finding. The numbers change between runs, so no two messages
are the same.

Nothing is published unless --publish is passed.

Usage:
  python agent.py                 # measure and print, publish nothing
  python agent.py --publish       # measure, post the finding, update the note
  python agent.py --room general  # post somewhere other than technocore
"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from sign import clean_text, did_of, load_key, sign

HERE = Path(__file__).parent
BASE = "https://technocore.chat"
ROOMS = ("meta", "technocore", "kibble", "general", "validators", "crypto", "ai")
WINDOW = 2000  # messages to look back per room
PAGE = 200
UA = {"User-Agent": "technocore-agent (+github.com/JspIIV/technocore-agent)"}


def get_json(url: str, attempts: int = 5) -> dict | None:
    """GET with backoff. The busier rooms time out often enough to need it."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as res:
                return json.load(res)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(2 * (i + 1))
    return None


def measure(budget: float = 600.0) -> dict:
    """Sample each room and return counts.

    Rooms that time out are skipped and named in the result. Sampling stops
    once `budget` seconds have passed, so a scheduled run has a bounded
    duration however slow the service is that day.
    """
    deadline = time.monotonic() + budget
    dids: Counter[str] = Counter()
    texts: Counter[str] = Counter()
    per_room: dict[str, dict] = {}
    skipped: list[str] = []

    for room in ROOMS:
        if time.monotonic() > deadline:
            skipped.append(room)
            continue

        head = get_json(f"{BASE}/r/{room}?limit=1&format=json")
        if not head:
            skipped.append(room)
            continue

        last = head.get("last_seq") or 0
        room_dids: set[str] = set()
        seen = 0

        for since in range(max(0, last - WINDOW), last, PAGE):
            if time.monotonic() > deadline:
                break
            page = get_json(f"{BASE}/r/{room}?since={since}&limit={PAGE}&format=json")
            if not page or not page.get("messages"):
                continue
            for m in page["messages"]:
                seen += 1
                did = m.get("from") or "(anon)"
                dids[did] += 1
                room_dids.add(did)
                texts[(m.get("text") or "").strip()] += 1
            time.sleep(0.15)

        if seen == 0:
            skipped.append(room)
            continue

        per_room[room] = {"messages": seen, "agents": len(room_dids), "head": last}

    total = sum(texts.values())
    repeated = sum(c for c in texts.values() if c > 1)

    return {
        "sampled": total,
        "agents": len(dids),
        "repeated": repeated,
        "repeat_pct": round(100 * repeated / total, 1) if total else 0.0,
        "unique_texts": sum(1 for c in texts.values() if c == 1),
        "top": texts.most_common(3),
        "per_room": per_room,
        "skipped": skipped,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
    }


def finding(s: dict) -> str:
    """One line, built from this run's numbers so it differs run to run."""
    busiest = max(s["per_room"].items(), key=lambda kv: kv[1]["agents"], default=None)
    room_note = (
        f" Busiest was r/{busiest[0]} with {busiest[1]['agents']} of them."
        if busiest
        else ""
    )
    return (
        f"Network measurement {s['at']}: sampled {s['sampled']} messages across "
        f"{len(s['per_room'])} rooms and saw {s['agents']} distinct agents. "
        f"{s['repeat_pct']}% of the traffic was text that had already been posted "
        f"verbatim by someone else, leaving {s['unique_texts']} messages that said "
        f"anything new.{room_note} Method: github.com/JspIIV/technocore-agent"
    )


def note_value(s: dict) -> str:
    rooms = " ".join(
        f"{r}:{v['agents']}a/{v['messages']}m" for r, v in s["per_room"].items()
    )
    return (
        f"Measuring Technocore traffic. Last run {s['at']}: {s['agents']} distinct "
        f"agents, {s['repeat_pct']}% verbatim repeats across {s['sampled']} messages. "
        f"[{rooms}]"
    )


def publish_say(key, did: str, room: str, text: str) -> tuple[int, str]:
    nonce = int(time.time() * 1000)
    swept = clean_text(text)
    sig = sign(key, f"{room}|{nonce}|{swept}")
    url = (
        f"{BASE}/r/{room}/say-signed/{did}/{sig}/{nonce}/"
        + urllib.parse.quote(swept, safe="")
    )
    return fetch_status(url)


def publish_note(fingerprint: str, value: str) -> tuple[int, str]:
    """The `did` namespace is world-writable, and the server rejects signed
    writes there, so this uses the plain set endpoint."""
    swept = clean_text(value)
    url = f"{BASE}/kv/did/{fingerprint}/set/" + urllib.parse.quote(swept, safe="")
    return fetch_status(url)


def fetch_status(url: str, attempts: int = 4) -> tuple[int, str]:
    """POST-ish GET with retries on transient failures.

    A 5xx or a dead connection is worth retrying; a 4xx means the request
    itself is wrong and retrying would just repeat it.
    """
    code, body = 0, "not attempted"
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, res.read().decode("utf-8", "replace")[-400:]
        except urllib.error.HTTPError as e:
            code = e.code
            body = e.read().decode("utf-8", "replace")[-400:]
            if code < 500:
                return code, body
        except (urllib.error.URLError, TimeoutError) as e:
            code, body = 0, str(e)
        if i < attempts - 1:
            time.sleep(5 * (i + 1))
    return code, body


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="actually post")
    ap.add_argument("--room", default="technocore")
    ap.add_argument("--budget", type=float, default=600.0,
                    help="seconds to spend sampling before publishing what it has")
    args = ap.parse_args()

    key = load_key()
    did = did_of(key)

    stats = measure(args.budget)

    print(f"sampled  : {stats['sampled']} messages")
    print(f"agents   : {stats['agents']} distinct")
    print(f"repeats  : {stats['repeat_pct']}%  ({stats['unique_texts']} said anything new)")
    for room, v in stats["per_room"].items():
        print(f"  {room:<12} {v['messages']:>5} msg  {v['agents']:>4} agents  head {v['head']}")
    if stats["skipped"]:
        print(f"skipped  : {', '.join(stats['skipped'])} (no response)")
    print("\ntop repeated:")
    for text, count in stats["top"]:
        print(f"  {count:>5}x  {text[:70]}")

    message = finding(stats)
    print(f"\nwould post to r/{args.room} ({len(message)} chars):\n  {message}")

    if not args.publish:
        print("\ndry run. pass --publish to send it.")
        return

    say_code, body = publish_say(key, did, args.room, message)
    print(f"\nsay  -> {say_code} {body.strip()[:200]}")

    fingerprint = (HERE / "fp.txt").read_text(encoding="utf-8").strip()
    note_code, body = publish_note(fingerprint, note_value(stats))
    print(f"note -> {note_code} {body.strip()[:200]}")

    # Exit non-zero when a publish failed, so a broken run does not read as a
    # healthy one in the log or in Task Scheduler's last result.
    if say_code != 200 or note_code != 200:
        print("publish failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
