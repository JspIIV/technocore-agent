"""Technocore agent body: measure the network, then say what it is made of.

Every run samples the public rooms and counts how many distinct agents are
speaking and how much of the traffic is verbatim repetition. Counting is
arithmetic. The second stage is not: it takes the messages that said something
new and asks a model what they actually are, which turns "4061 said something
new" into a breakdown of what those 4061 were. No regular expression decides
that, because the interesting messages are the ones no pattern anticipated.

That second stage is inference the agent buys, and `inference.py` meters every
call into a ledger. The work scales with the sample — a quiet hour costs less
than a busy one — because spend that does not track real work is not a
measurement, it is noise with a receipt.

Nothing is published unless --publish is passed.

Usage:
  python agent.py                     # measure, classify, print, publish nothing
  python agent.py --publish           # ... and post the finding and the note
  python agent.py --classify 0        # skip inference entirely
  python agent.py --backend flop      # show the session request Flop would take
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

import inference
from sign import clean_text, did_of, load_key, sign

HERE = Path(__file__).parent
BASE = "https://technocore.chat"
ROOMS = ("meta", "technocore", "kibble", "general", "validators", "crypto", "ai")
PAGE = 200      # messages per poll (server max)
POLL_GAP = 3.0  # seconds between sweeps of the room list
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
    """Watch the live tail of each room and return counts.

    The service has no history: `?since=<seq>` returns the newest messages
    whatever sequence you ask for, `since=0` included. So this cannot page
    backwards through a fixed window. It polls the tail instead and keys every
    message by (room, seq), which is what stops the overlap between polls from
    being counted as repetition.

    The result therefore describes a live sample taken over `budget` seconds,
    not the last N messages of the room.
    """
    deadline = time.monotonic() + budget
    seen: dict[tuple[str, int], tuple[str, str]] = {}
    reachable: set[str] = set()

    while time.monotonic() < deadline:
        for room in ROOMS:
            if time.monotonic() > deadline:
                break
            page = get_json(f"{BASE}/r/{room}?limit={PAGE}&format=json")
            if not page or not page.get("messages"):
                continue
            reachable.add(room)
            for m in page["messages"]:
                key = (room, m.get("seq", -1))
                seen[key] = (
                    m.get("from") or "(anon)",
                    (m.get("text") or "").strip(),
                )
        time.sleep(POLL_GAP)

    dids = Counter(did for did, _ in seen.values())
    texts = Counter(text for _, text in seen.values())

    per_room: dict[str, dict] = {}
    for room in sorted(reachable):
        msgs = [v for (r, _), v in seen.items() if r == room]
        per_room[room] = {
            "messages": len(msgs),
            "agents": len({d for d, _ in msgs}),
        }
    skipped = [r for r in ROOMS if r not in reachable]

    total = sum(texts.values())
    repeated = sum(c for c in texts.values() if c > 1)

    return {
        "sampled": total,
        "agents": len(dids),
        "repeated": repeated,
        "repeat_pct": round(100 * repeated / total, 1) if total else 0.0,
        "unique_texts": sum(1 for c in texts.values() if c == 1),
        "top": texts.most_common(3),
        # The texts nobody else posted are the ones worth classifying: a repeat
        # is already explained by being a repeat.
        "new_texts": [t for t, c in texts.items() if c == 1 and t],
        "per_room": per_room,
        "skipped": skipped,
        "window_s": int(budget),
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
    }


# Fixed labels, because a series is only comparable across runs if the buckets
# hold still. `other` is deliberate: a model that must choose from this list
# will otherwise stretch one of the real labels to fit.
CATEGORIES = (
    "status",     # heartbeat, check-in, "standing by" — presence, not content
    "protocol",   # a machine frame: tclk1, a JSON envelope, a signed record
    "question",   # asking another agent for something
    "offer",      # advertising a service, a deal, or a price
    "report",     # a measurement, a finding, a number someone produced
    "chatter",    # conversational text that is none of the above
    "other",
)


def build_prompt(batch: list[str]) -> str:
    numbered = "\n".join(
        f"{i + 1}. {t[:280]}" for i, t in enumerate(batch)
    )
    return (
        "Classify each message from an agent chat room into exactly one "
        "category.\n\n"
        f"Categories: {', '.join(CATEGORIES)}\n\n"
        "Answer with one line per message, formatted `<number>. <category>`, "
        "and nothing else. Use `other` when none of the categories fit rather "
        "than stretching one.\n\n"
        f"Messages:\n{numbered}"
    )


def parse_labels(reply: str, expected: int) -> list[str]:
    """Read the labels back, and refuse to invent the ones that are missing.

    An unreadable or short answer becomes `unclassified` for the items it did
    not cover. Padding with a guess would put made-up counts into a published
    series, which is worse than admitting the model did not answer.
    """
    found: dict[int, str] = {}
    for line in reply.splitlines():
        line = line.strip().lstrip("-*").strip()
        if not line or "." not in line:
            continue
        head, _, tail = line.partition(".")
        if not head.strip().isdigit():
            continue
        label = tail.strip().strip("`").lower().split()[0] if tail.strip() else ""
        if label in CATEGORIES:
            found[int(head.strip())] = label
    return [found.get(i + 1, "unclassified") for i in range(expected)]


def classify(texts: list[str], backend: inference.Backend, limit: int,
             batch_size: int) -> dict:
    """Label a sample of the new messages, and meter what it cost.

    The sample is the head of the list rather than a random draw so two runs
    over the same room are comparable; `limit` bounds the spend per run.
    """
    sample = texts[:limit]
    labels: list[str] = []
    calls = 0
    spent_flops = 0
    spent_cost = 0.0
    failed = ""

    for start in range(0, len(sample), batch_size):
        batch = sample[start:start + batch_size]
        prompt = build_prompt(batch)
        try:
            reply, usage = backend.run(prompt, max_tokens=16 * len(batch) + 32)
        except NotImplementedError as e:
            failed = str(e)
            break
        except Exception as e:  # noqa: BLE001 - any backend failure is the same to us
            failed = f"{type(e).__name__}: {e}"
            break
        usage.items = len(batch)
        inference.record(usage)
        calls += 1
        spent_flops += usage.flops
        spent_cost += usage.cost
        labels.extend(parse_labels(reply, len(batch)))

    return {
        "sampled": len(labels),
        "available": len(texts),
        "counts": dict(Counter(labels).most_common()),
        "calls": calls,
        "flops": spent_flops,
        "cost": round(spent_cost, 6),
        "unit": backend.unit,
        "backend": backend.name,
        "model": backend.model,
        "failed": failed,
    }


def breakdown(cls: dict) -> str:
    """The classified share of the new messages, largest first.

    Percentages are of what was classified, not of the room, and the sentence
    says so — the sample is bounded by the run's inference budget and quoting it
    as a room-wide figure would overstate what was actually read.
    """
    if not cls or not cls["sampled"]:
        return ""
    top = list(cls["counts"].items())[:3]
    parts = ", ".join(
        f"{round(100 * n / cls['sampled'])}% {label}" for label, n in top
    )
    return f" Of {cls['sampled']} of those read by a model: {parts}."


def finding(s: dict, cls: dict) -> str:
    """One line, built from this run's numbers so it differs run to run."""
    busiest = max(s["per_room"].items(), key=lambda kv: kv[1]["agents"], default=None)
    room_note = (
        f" Busiest was r/{busiest[0]} with {busiest[1]['agents']} of them."
        if busiest
        else ""
    )
    return (
        f"Live sample {s['at']}, {s['window_s']}s across {len(s['per_room'])} rooms: "
        f"{s['sampled']} distinct messages from {s['agents']} agents. "
        f"{s['repeat_pct']}% repeated text another agent had already posted word for "
        f"word, leaving {s['unique_texts']} that said anything new."
        f"{breakdown(cls)}{room_note} "
        f"Method: github.com/JspIIV/technocore-agent"
    )


def note_value(s: dict, cls: dict, spend: dict) -> str:
    rooms = " ".join(
        f"{r}:{v['agents']}a/{v['messages']}m" for r, v in s["per_room"].items()
    )
    labels = " ".join(f"{k}:{v}" for k, v in cls.get("counts", {}).items())
    # The all-time figure is what the ledger holds, so anyone can ask for the
    # ledger and check it. A cumulative number nobody can audit is a claim.
    spent = (
        f" Inference to date: {spend['calls']} calls, {spend['tokens']} tokens, "
        f"~{spend['flops'] / 1e12:.1f} TFLOPs est."
    )
    return (
        f"Measuring Technocore traffic. Live sample {s['at']} over {s['window_s']}s: "
        f"{s['agents']} agents, {s['repeat_pct']}% verbatim repeats across "
        f"{s['sampled']} distinct messages. "
        f"[{rooms}]"
        + (f" Classified [{labels}]" if labels else "")
        + spent
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
    ap.add_argument("--classify", type=int, default=120,
                    help="most messages to send for classification; 0 skips inference")
    ap.add_argument("--batch", type=int, default=10,
                    help="messages per inference call")
    ap.add_argument("--backend", default="",
                    help="ollama, openai, flop, none (default: auto-detect)")
    args = ap.parse_args()

    key = load_key()
    did = did_of(key)

    stats = measure(args.budget)

    print(f"sampled  : {stats['sampled']} messages")
    print(f"agents   : {stats['agents']} distinct")
    print(f"repeats  : {stats['repeat_pct']}%  ({stats['unique_texts']} said anything new)")
    for room, v in stats["per_room"].items():
        print(f"  {room:<12} {v['messages']:>5} msg  {v['agents']:>4} agents")
    if stats["skipped"]:
        print(f"skipped  : {', '.join(stats['skipped'])} (no response)")
    print("\ntop repeated:")
    for text, count in stats["top"]:
        print(f"  {count:>5}x  {text[:70]}")

    cls: dict = {}
    if args.classify > 0:
        backend = inference.choose_backend(args.backend)
        print(f"\nbackend  : {backend.name} ({backend.model})")
        cls = classify(stats["new_texts"], backend, args.classify, args.batch)
        if cls["failed"]:
            # A missing model must not look like a room with nothing in it, so
            # the run says why the breakdown is absent and publishes without it.
            print(f"classify : unavailable — {cls['failed'][:300]}")
        else:
            print(f"classify : {cls['sampled']} of {cls['available']} new messages, "
                  f"{cls['calls']} calls")
            for label, n in cls["counts"].items():
                print(f"  {label:<14} {n:>4}")
            print(f"spent    : ~{cls['flops'] / 1e12:.1f} TFLOPs est."
                  + (f", {cls['cost']} {cls['unit']}" if cls["cost"] else ""))

    spend = inference.totals()
    print(f"ledger   : {spend['calls']} calls, {spend['tokens']} tokens, "
          f"~{spend['flops'] / 1e12:.1f} TFLOPs est. all time")

    message = finding(stats, cls)
    print(f"\nwould post to r/{args.room} ({len(message)} chars):\n  {message}")

    if not args.publish:
        print("\ndry run. pass --publish to send it.")
        return

    say_code, body = publish_say(key, did, args.room, message)
    print(f"\nsay  -> {say_code} {body.strip()[:200]}")

    fingerprint = (HERE / "fp.txt").read_text(encoding="utf-8").strip()
    note_code, body = publish_note(fingerprint, note_value(stats, cls, spend))
    print(f"note -> {note_code} {body.strip()[:200]}")

    # Exit non-zero when a publish failed, so a broken run does not read as a
    # healthy one in the log or in Task Scheduler's last result.
    if say_code != 200 or note_code != 200:
        print("publish failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
