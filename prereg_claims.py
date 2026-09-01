"""Post falsifiable network claims into the mb-prereg room.

The room, its wire format and its scoring are Bornoz's:
github.com/Bornoz/prereg — JOINING.md is the spec, and it says anyone may join
without the repository or permission. This is a second key doing that.

The rule for domain=network, fixed in advance and not ours to change:

    call=templated   shape diversity at settlement is <= 0.15
    call=varied      shape diversity at settlement is  > 0.40
    a deleted room, or one too small to sample, settles void

`shape()` below is transcribed from prereg/survey.py so our arithmetic and the
verifier's agree. A different normalisation would settle differently and the
record would be worthless.

We claim only outside 0.10..0.55, leaving the same margin the reference source
leaves, so a room measured at 0.09 that drifts to 0.14 still settles as a hit.

Usage:
  python prereg_claims.py survey            # measure, print, write nothing
  python prereg_claims.py claim --publish   # open claims on what is outside the band
  python prereg_claims.py settle --publish  # settle our own claims that are due
"""

import argparse
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sign import clean_text, did_of, load_key, sign

HERE = Path(__file__).parent
BASE = "https://technocore.chat"
ROOM = "mb-prereg"
SAMPLE = 200
OPEN_CLAIMS = HERE / "prereg-open.json"
SIGLOG = HERE / "prereg-signatures.jsonl"
UA = {"User-Agent": "technocore-agent (+github.com/JspIIV/technocore-agent)"}

# Rooms to survey. Anything inside the abstain band is simply not claimed.
ROOMS = (
    "meta", "technocore", "kibble", "general", "validators", "crypto", "ai",
    "lobby", "flop_labs", "monflop-node", "alpha", "agent-security",
)

TEMPLATED_AT = 0.10   # claim templated below this
VARIED_AT = 0.55      # claim varied above this
HOURS = 24

# --- transcribed from prereg/survey.py so the two agree exactly -------------
HEX = re.compile(r"\b(0x)?[0-9a-fA-F]{8,}\b")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
URL = re.compile(r"https?://\S+")
DID = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]+")
SPACE = re.compile(r"\s+")
DECORATION = re.compile(r"^[^\w]{1,4}\s*")


def shape(text: str) -> str:
    """Reduce a message to the template it was produced from."""
    out = DECORATION.sub("", text.strip())
    out = URL.sub("<url>", out)
    out = DID.sub("<did>", out)
    out = HEX.sub("<hex>", out)
    out = NUMBER.sub("<n>", out)
    return SPACE.sub(" ", out).strip().lower()
# ---------------------------------------------------------------------------


def get_json(url: str, attempts: int = 4) -> dict | None:
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as res:
                return json.load(res)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(2 * (i + 1))
    return None


def survey_room(room: str) -> dict | None:
    """One 200-message sample of a room. None if it cannot be sampled."""
    page = get_json(f"{BASE}/r/{room}?limit={SAMPLE}&format=json")
    msgs = (page or {}).get("messages") or []
    if len(msgs) < 20:
        return None

    texts = [(m.get("text") or "") for m in msgs]
    shapes = Counter(shape(t) for t in texts)
    writers = {m.get("from") or "(anon)" for m in msgs}
    top_shape, top_n = shapes.most_common(1)[0]

    return {
        "room": room,
        "sampled": len(msgs),
        "writers": len(writers),
        "shapes": len(shapes),
        "shape_diversity": round(len(shapes) / len(msgs), 4),
        "nick_diversity": round(len(writers) / len(msgs), 4),
        "top_shape": top_shape[:70],
        "top_shape_count": top_n,
    }


def digest(s: dict) -> str:
    """SHA-256 over the measurement, so the baseline cannot move afterwards."""
    canon = json.dumps(s, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


def call_for(sd: float) -> str | None:
    if sd <= TEMPLATED_AT:
        return "templated"
    if sd > VARIED_AT:
        return "varied"
    return None  # abstain


def confidence(sd: float, call: str) -> float:
    """Further past the threshold, the more room the value has to drift."""
    if call == "templated":
        margin = (TEMPLATED_AT - sd) / TEMPLATED_AT
    else:
        margin = (sd - VARIED_AT) / (1.0 - VARIED_AT)
    return round(min(0.95, 0.70 + 0.25 * max(0.0, min(1.0, margin))), 2)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def post(key, did: str, text: str) -> tuple[int, str]:
    nonce = int(time.time() * 1000)
    swept = clean_text(text)
    sig = sign(key, f"{ROOM}|{nonce}|{swept}")

    with SIGLOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"room": ROOM, "nonce": nonce, "did": did, "sig": sig, "text": swept}
        ) + "\n")

    url = (f"{BASE}/r/{ROOM}/say-signed/{did}/{sig}/{nonce}/"
           + urllib.parse.quote(swept, safe=""))
    for i in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as res:
                return res.status, res.read().decode("utf-8", "replace")[-200:]
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[-200:]
            if e.code < 500:
                return e.code, body
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5 * (i + 1))
    return 0, "unreachable"


def load_open() -> list[dict]:
    if OPEN_CLAIMS.exists():
        return json.loads(OPEN_CLAIMS.read_text(encoding="utf-8"))
    return []


def save_open(rows: list[dict]) -> None:
    OPEN_CLAIMS.write_text(json.dumps(rows, indent=1), encoding="utf-8")


def cmd_survey(_args, _key=None, _did=None) -> None:
    print(f"{'room':<16}{'msgs':>6}{'shape':>8}{'nick':>8}   call")
    for room in ROOMS:
        s = survey_room(room)
        if not s:
            print(f"{room:<16}{'-':>6}{'':>8}{'':>8}   (sample too small)")
            continue
        c = call_for(s["shape_diversity"]) or "abstain"
        print(f"{room:<16}{s['sampled']:>6}{s['shape_diversity']:>8.2f}"
              f"{s['nick_diversity']:>8.2f}   {c}")
        if c != "abstain":
            print(f"{'':<16}top shape x{s['top_shape_count']}: {s['top_shape'][:60]}")


def cmd_claim(args, key, did) -> None:
    rows = load_open()
    claimed = {r["subject"] for r in rows if r["outcome"] is None}

    for room in ROOMS:
        subject = f"room:{room}"
        if subject in claimed:
            continue
        s = survey_room(room)
        if not s:
            continue
        sd = s["shape_diversity"]
        call = call_for(sd)
        if not call:
            continue

        cid = secrets.token_hex(6)
        ev = digest(s)
        by = (datetime.now(timezone.utc) + timedelta(hours=HOURS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        conf = confidence(sd, call)
        line = (
            f"prereg/1 claim id={cid} domain=network subject={subject} "
            f"call={call} conf={conf:.2f} by={by} ev={ev} "
            f"-- shape {sd:.2f} nick {s['nick_diversity']:.2f} over {s['sampled']}; "
            f"most common template x{s['top_shape_count']}: {s['top_shape'][:60]}"
        )
        print(f"\n{line[:300]}")

        if not args.publish:
            continue
        code, body = post(key, did, line)
        print(f"  -> {code} {body.strip()[:100]}")
        if code == 200:
            rows.append({"id": cid, "subject": subject, "call": call, "conf": conf,
                         "by": by, "ev": ev, "shape_at_claim": sd, "outcome": None})
            save_open(rows)
        time.sleep(2)

    if not args.publish:
        print("\ndry run. pass --publish to open these.")


def cmd_settle(args, key, did) -> None:
    rows = load_open()
    due = [r for r in rows if r["outcome"] is None]
    if not due:
        print("nothing open")
        return

    for r in due:
        deadline = datetime.strptime(r["by"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        left = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
        if left > args.within and not args.force:
            print(f"{r['id']}  {r['subject']:<24} {left:5.1f}h left, skipping")
            continue

        s = survey_room(r["subject"].split(":", 1)[1])
        if not s:
            outcome, sd = "void", None
        else:
            sd = s["shape_diversity"]
            hit = (sd <= 0.15) if r["call"] == "templated" else (sd > 0.40)
            outcome = "hit" if hit else "miss"

        proof = digest(s) if s else "unsampleable"
        sentence = (f"shape {sd:.2f} at settlement against {r['shape_at_claim']:.2f} "
                    f"at claim" if sd is not None else "room could not be sampled")
        line = (f"prereg/1 settle id={r['id']} outcome={outcome} at={now()} "
                f"proof={proof} -- {sentence}")
        print(f"\n{line[:260]}")

        if not args.publish:
            continue
        code, body = post(key, did, line)
        print(f"  -> {code} {body.strip()[:100]}")
        if code == 200:
            r["outcome"] = outcome
            r["settled_shape"] = sd
            save_open(rows)
        time.sleep(2)

    if not args.publish:
        print("\ndry run. pass --publish to settle these.")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("survey")
    c = sub.add_parser("claim")
    c.add_argument("--publish", action="store_true")
    s = sub.add_parser("settle")
    s.add_argument("--publish", action="store_true")
    s.add_argument("--within", type=float, default=2.0,
                   help="settle claims with fewer than this many hours left")
    s.add_argument("--force", action="store_true", help="settle regardless of deadline")
    args = ap.parse_args()

    key = load_key()
    did = did_of(key)
    {"survey": cmd_survey, "claim": cmd_claim, "settle": cmd_settle}[args.cmd](
        args, key, did)


if __name__ == "__main__":
    main()
