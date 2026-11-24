"""Offline checks for the Flop spend path: no network, no real keys.

    python test_flopspend.py
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import inference as inf

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, bool(ok), note))


class FakeRun:
    """Stands in for subprocess.run and remembers what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cmd, capture_output, text, timeout, env):
        self.calls.append((cmd, env))
        out = self.replies.pop(0)
        if isinstance(out, Exception):
            raise out
        stdout, code = out
        return subprocess.CompletedProcess(cmd, code, stdout, "npm warn deprecated something")


def make_cli(tmp: Path, runner) -> inf.FlopCli:
    ks = tmp / "chain-key.json"
    ks.write_text(json.dumps({"address": "5G3jwp9sAAXLtH9mVzZ96ftJDCijQCoJbScs9RqTpAcGfZzT"}))
    pw = tmp / "chain-key.pass"
    pw.write_text("not-a-real-password\n")
    cli = inf.FlopCli(network="devnet", package="@flop-labs/cli@0.8.1", keystore=str(ks),
                      password_file=str(pw), timeout=5, runner=runner)
    cli.ready = lambda: True  # npx may be absent where the tests run
    return cli


MINER = [{"miner": "5Minerxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "endpoint": "https://m.example",
          "model": "qwen/qwen3-8b"}]

GOOD = json.dumps({"ok": True, "reply": "1. report\n2. status", "servingMiner": MINER[0]["miner"],
                   "channelId": "0xabc", "settlementTx": "0xdef", "minerPaid": "990000000000000000",
                   "usage": {"prompt_tokens": 40, "completion_tokens": 8}})


# What CLI 0.8.1 actually prints for a session: no price, and `settle_tx`.
GOOD_081 = json.dumps({"reply": "1. report", "miner": MINER[0]["miner"], "channel": "0xabc",
                       "settle_tx": "0xfeed", "cooperative": True, "verified_turns": 1})


def bal(n: float) -> tuple[str, int]:
    """One `flop balance` answer."""
    return json.dumps({"free": "0", "reserved": "0", "total": str(round(n * 10**6) * 10**12)}), 0


def main() -> int:
    # parse_cli_json survives npm noise and reports non-JSON plainly
    d = inf.parse_cli_json('npm warn x\n{"ok": false, "code": "X", "message": "m"}\n', 1)
    check("parse: JSON inside npm noise", d.get("code") == "X" and d["ok"] is False)
    d = inf.parse_cli_json("", 1, 'npm warn x\n{"ok": false, "code": "RATE_LIMITED", "message": "m"}\n')
    check("parse: error JSON on stderr", d.get("code") == "RATE_LIMITED" and d["ok"] is False)
    noisy = ('{\n  "ok": false,\n  "code": "RPC_UNREACHABLE",\n  "message": "m"\n}\n'
             'RPC-CORE: x\nFailed WS Request: {"method":"state_getMetadata","params":[]}\n    at #on (x.js:1:2)\n')
    d = inf.parse_cli_json("", 1, noisy)
    check("parse: result before Polkadot log noise", d.get("code") == "RPC_UNREACHABLE")
    d = inf.parse_cli_json("garbage", 2, "boom")
    check("parse: non-JSON becomes ok=False", d["ok"] is False and d["code"] == "NO_JSON")
    check("flop_amount raw units", abs(inf.flop_amount("990000000000000000") - 0.99) < 1e-12)
    check("flop_amount decimal", inf.flop_amount("0.25") == 0.25)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        ledger = tmp / "spend.jsonl"
        grants = tmp / "grants.jsonl"
        grants.write_text(json.dumps({"t": 1, "ok": True, "network": "devnet", "amount": 25.0}) + "\n")
        empty = tmp / "empty.jsonl"

        # no miners: refuse, do not guess an endpoint
        b = inf.FlopBackend("qwen/qwen3-8b", cli=make_cli(tmp, FakeRun([])), miners=[],
                            max_flop=1, daily_budget=5, ledger=ledger, faucet_log=grants)
        try:
            b.run("p", 10)
            check("no miner refuses", False, "ran")
        except NotImplementedError:
            check("no miner refuses", True)

        # a good session: command shape, password only in env, ledger fields
        run = FakeRun([bal(25), (GOOD, 0)])
        b = inf.FlopBackend("qwen/qwen3-8b", cli=make_cli(tmp, run), miners=MINER,
                            max_flop=1, daily_budget=5, ledger=ledger, faucet_log=grants)
        reply, usage = b.run("classify these", 40)
        cmd, env = run.calls[1]
        check("session run command", cmd[cmd.index("flop") + 1:cmd.index("flop") + 3] == ["--network", "devnet"]
              and "session" in cmd and "--max-flop" in cmd and cmd[cmd.index("--max-flop") + 1] == "1"
              and "--endpoint" in cmd and cmd[-1] == "--json")
        check("password not on the command line", "not-a-real-password" not in " ".join(cmd)
              and env.get("FLOP_KEY_PASSWORD") == "not-a-real-password")
        check("reply read", reply.startswith("1. report"))
        check("ledger fields", usage.path == "direct" and usage.channel == "0xabc"
              and usage.settlement == "0xdef" and abs(usage.cost - 0.99) < 1e-12
              and usage.prompt_tokens == 40 and usage.unit == "FLOP")
        inf.record(usage, ledger)
        check("spent_today counts it", abs(inf.spent_today(ledger) - 0.99) < 1e-9)

        # 0.99 spent, daily cap 1.0 -> nothing worth opening
        b = inf.FlopBackend("qwen/qwen3-8b", cli=make_cli(tmp, FakeRun([])), miners=MINER,
                            max_flop=1, daily_budget=1.0, ledger=ledger, faucet_log=grants)
        try:
            b.run("p", 10)
            check("budget stops", False, "ran")
        except inf.BudgetReached:
            check("budget stops", True)

        # CLI 0.8.1 shape: the price comes off the wallet
        def backend(run, log):
            return inf.FlopBackend("qwen/qwen3-8b", cli=make_cli(tmp, run), miners=MINER, max_flop=1,
                                   ledger=empty, faucet_log=log)

        _, u = backend(FakeRun([bal(25), (GOOD_081, 0), bal(24.8)]), grants).run("p", 10)
        check("0.8.1: price from balance", abs(u.cost - 0.2) < 1e-9 and u.cost_basis == "balance"
              and u.settlement == "0xfeed" and u.channel == "0xabc" and u.network == "devnet")
        _, u = backend(FakeRun([bal(25), (GOOD_081, 0), ("", 1)]), grants).run("p", 10)
        check("0.8.1: unreadable balance is a bound", u.cost == 1 and u.cost_basis == "escrow-cap")
        _, u = backend(FakeRun([bal(25), (GOOD_081, 0), bal(50)]), grants).run("p", 10)
        check("0.8.1: grant mid-session is no price", u.cost_basis == "escrow-cap")

        # the budget is what the faucet gave, per network
        check("budget: grant minus spend", abs(inf.budget_left("devnet", 0, ledger, grants)
              - (25 - 0.99 - inf.FEE_RESERVE)) < 1e-9)
        check("budget: other network has none", inf.budget_left("testnet", 0, ledger, grants) < 0)
        try:
            backend(FakeRun([]), empty).run("p", 10)
            check("budget: no grant, no spend", False)
        except inf.BudgetReached:
            check("budget: no grant, no spend", True)
        small = tmp / "small.jsonl"
        small.write_text(json.dumps({"t": 1, "ok": True, "network": "devnet", "amount": 0.45}) + "\n")
        run = FakeRun([bal(1), (GOOD_081, 0), bal(0.9)])
        backend(run, small).run("p", 10)
        c = run.calls[1][0]
        check("budget: last escrow shrinks to fit", c[c.index("--max-flop") + 1] == "0.4")

        # testnet stays off until the official genesis is pinned and served
        pin = "0x" + "ab" * 32

        def tcli(replies):
            c = make_cli(tmp, FakeRun(replies))
            c.network = "testnet"
            return c

        os.environ.pop("FLOP_GENESIS", None)
        c = tcli([])
        got = inf.claim_faucet(c, tmp / "tf.jsonl", every_hours=24)
        check("testnet: no genesis, no claim", not got["ok"] and not c.runner.calls)
        tg = tmp / "tg.jsonl"
        tg.write_text(json.dumps({"t": 1, "ok": True, "network": "testnet", "amount": 5}) + "\n")
        c = tcli([])
        try:
            inf.FlopBackend("qwen/qwen3-8b", cli=c, miners=MINER, ledger=empty, faucet_log=tg).run("p", 10)
            check("testnet: no genesis, no spend", False)
        except NotImplementedError:
            check("testnet: no genesis, no spend", not c.runner.calls)
        os.environ["FLOP_GENESIS"] = pin
        check("testnet: wrong chain refused",
              tcli([(json.dumps({"rpc": {"genesis_hash": "0x00"}}), 0)]).genesis_error())
        check("testnet: unreachable refused",
              tcli([(json.dumps({"rpc": {"reachable": False}}), 0)]).genesis_error())
        check("testnet: right chain accepted",
              not tcli([(json.dumps({"rpc": {"genesis_hash": pin}}), 0)]).genesis_error())
        os.environ.pop("FLOP_GENESIS", None)

        # a failed session stops the rest of the run (escrow safety)
        run = FakeRun([bal(25), (json.dumps({"ok": False, "code": "RPC_UNREACHABLE", "message": "x"}), 1)])
        b = inf.FlopBackend("qwen/qwen3-8b", cli=make_cli(tmp, run), miners=MINER,
                            max_flop=1, daily_budget=50, ledger=ledger, faucet_log=grants)
        try:
            b.run("p", 10)
            check("failure raises", False)
        except RuntimeError:
            check("failure raises", True)
        try:
            b.run("p", 10)
            check("no retry after failure", False)
        except NotImplementedError:
            check("no retry after failure", len(run.calls) == 2)

        # timeout comes back as ok=False, not an exception
        cli = make_cli(tmp, FakeRun([subprocess.TimeoutExpired("npx", 5)]))
        check("timeout handled", cli.call("health")["code"] == "TIMEOUT")

        # faucet: claims once, then skips inside the window
        log = tmp / "faucet.jsonl"
        run = FakeRun([(json.dumps({"ok": True, "amount": "25"}), 0)])
        cli = make_cli(tmp, run)
        first = inf.claim_faucet(cli, log, every_hours=24)
        second = inf.claim_faucet(cli, log, every_hours=24)
        check("faucet claims", first.get("ok") and run.calls and "faucet" in run.calls[0][0]
              and run.calls[0][0][-2] == "5G3jwp9sAAXLtH9mVzZ96ftJDCijQCoJbScs9RqTpAcGfZzT")
        check("faucet amount logged", inf.rows_of(log)[0]["amount"] == 25)
        check("faucet skips inside window", second.get("skipped") and len(run.calls) == 1)
        log2 = tmp / "faucet2.jsonl"
        run2 = FakeRun([(json.dumps({"ok": False, "code": "RATE_LIMITED",
                                     "message": "HTTP 429 - Address funded recently; retry in 1439 min"}), 1)])
        cli2 = make_cli(tmp, run2)
        got = inf.claim_faucet(cli2, log2, every_hours=24)
        again = inf.claim_faucet(cli2, log2, every_hours=24)
        check("faucet 'funded recently' counts as done", got.get("ok") and again.get("skipped")
              and len(run2.calls) == 1)
        run3 = FakeRun([(json.dumps({"ok": False, "code": "FAUCET_OUTCOME_UNKNOWN", "message": "x"}), 1)])
        cli3 = make_cli(tmp, run3)
        inf.claim_faucet(cli3, tmp / "faucet3.jsonl", every_hours=24)
        again3 = inf.claim_faucet(cli3, tmp / "faucet3.jsonl", every_hours=24)
        check("faucet unknown outcome is not retried", again3.get("skipped") and len(run3.calls) == 1)
        check("faucet unsigned (no password)", "FLOP_KEY_PASSWORD" not in run.calls[0][1]
              or run.calls[0][1].get("FLOP_KEY_PASSWORD") != "not-a-real-password")

        # gateway fallback refuses past the budget without calling out
        gw = inf.FlopGatewayBackend(key_file=str(tmp / "none.json"), daily_budget=0.5, ledger=ledger, faucet_log=grants)
        try:
            gw.run("p", 10)
            check("gateway budget", False)
        except inf.BudgetReached:
            check("gateway budget", True)

        # totals keeps FLOP apart from other units
        (tmp / "mixed.jsonl").write_text(
            json.dumps({"unit": "local", "cost": 0, "at": "x"}) + "\n" + ledger.read_text())
        tot = inf.totals(tmp / "mixed.jsonl")
        check("totals separates FLOP", tot["flop_calls"] == 1 and abs(tot["flop_cost"] - 0.99) < 1e-9)

    width = max(len(n) for n, _, _ in results)
    for name, ok, note in results:
        print(f"{'OK  ' if ok else 'FAIL'} {name:<{width}}  {note}")
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
