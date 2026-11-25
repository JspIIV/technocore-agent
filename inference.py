"""The inference this agent buys, and the ledger that proves it.

Counting messages is arithmetic and needs no model. Saying what those messages
*are* does: the categories below cannot be decided by a regular expression,
because agents invent their own phrasing and the interesting cases are exactly
the ones no pattern anticipated. That is the work this module runs.

Backends are interchangeable on purpose. Today a local or hosted model serves
the request. On the Flop network the same call becomes a session that pays a miner to
run it, so the swap is a backend, not a rewrite: `FlopBackend` drives the
official Flop CLI and records the channel and settlement of every session.

Every call is metered into `spend.jsonl`, one row each. The network rewards what
an agent spends on inference, and a spend claim nobody can audit is worth
nothing, so the ledger is written before the figure is ever published.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

HERE = Path(__file__).parent
LEDGER = HERE / "spend.jsonl"
FAUCET_LOG = HERE / "faucet.jsonl"
TESTNET_FAUCET = "https://faucet.flop.network"
CLAIM_PATH = "/v1/testnet/agent-claims"

# Seconds to wait before asking a busy RPC again, and before asking the faucet
# whether a claim it has included is final.
RPC_WAITS = (5, 20)
FINAL_WAITS = (20, 40)

# A forward pass costs about two floating-point operations per parameter per
# token. It is an estimate, not a measurement, and it is labelled as one
# wherever it is published — but a Flop session request has to name the compute
# it wants in FLOPs, so the agent needs this number before the network exists.
FLOPS_PER_PARAM_PER_TOKEN = 2

# Parameter counts for models this agent is likely to be pointed at. An unknown
# model falls back to the default rather than guessing high and inflating every
# figure that follows.
PARAMS = {
    # Flop registry slugs first: the more specific prefix has to win.
    "qwen/qwen2.5-0.5b": 500_000_000,
    "qwen/qwen3-8b": 8_000_000_000,
    "qwen/qwen3.8-27b": 27_000_000_000,
    "llama3.2": 3_000_000_000,
    "llama3.1": 8_000_000_000,
    "qwen2.5": 7_000_000_000,
    "mistral": 7_000_000_000,
    "phi3": 3_800_000_000,
    "gemma2": 9_000_000_000,
}
DEFAULT_PARAMS = 7_000_000_000

# Kept back for transaction fees, and the smallest escrow worth opening.
FEE_RESERVE = 0.05
MIN_ESCROW = 0.05


@dataclass
class Usage:
    """What one inference call consumed."""

    at: str
    backend: str
    model: str
    items: int
    prompt_tokens: int
    completion_tokens: int
    flops: int
    cost: float
    unit: str
    # Set only for Flop sessions, so anyone can look each row up on chain.
    path: str = ""
    miner: str = ""
    channel: str = ""
    settlement: str = ""
    network: str = ""
    # Where `cost` came from: "reported" by the CLI, "balance" (the wallet's
    # change over the session, so fees are included), or "escrow-cap" (only the
    # upper bound is known).
    cost_basis: str = ""


class BudgetReached(Exception):
    """The day's spend cap is used up. Not a failure: the run stops buying and
    publishes what it has, so the cap is visible instead of silently exceeded."""


def params_for(model: str) -> int:
    base = model.split(":")[0].lower()
    for known, n in PARAMS.items():
        if base.startswith(known):
            return n
    return DEFAULT_PARAMS


def estimate_tokens(text: str) -> int:
    """Roughly four characters per token.

    The backends that report real token counts overwrite this; it exists so a
    backend that reports nothing still lands a defensible number in the ledger
    instead of a zero that would understate the spend.
    """
    return max(1, len(text) // 4)


def post_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


class Backend:
    """A place that will run a prompt and charge for it."""

    name = "none"
    model = "-"
    unit = "none"

    def available(self) -> bool:
        return False

    def run(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        raise NotImplementedError

    def _usage(self, prompt: str, reply: str, items: int,
               prompt_tokens: int = 0, completion_tokens: int = 0,
               cost: float = 0.0) -> Usage:
        pt = prompt_tokens or estimate_tokens(prompt)
        ct = completion_tokens or estimate_tokens(reply)
        return Usage(
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            backend=self.name,
            model=self.model,
            items=items,
            prompt_tokens=pt,
            completion_tokens=ct,
            flops=FLOPS_PER_PARAM_PER_TOKEN * params_for(self.model) * (pt + ct),
            cost=cost,
            unit=self.unit,
        )


class OllamaBackend(Backend):
    """A model on a GPU you can reach over HTTP.

    This is the closest thing available to what Flop will do: the agent hands a
    job to a machine that owns the hardware and gets a completion back. Only the
    settlement is missing.
    """

    name = "ollama"
    unit = "local"

    def __init__(self, base: str, model: str) -> None:
        self.base = base.rstrip("/")
        self.model = model

    def available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base}/api/tags")
            with urllib.request.urlopen(req, timeout=4) as res:
                json.load(res)
            return True
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False

    def run(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        out = post_json(
            f"{self.base}/api/chat",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                # Temperature zero because the same room should classify the
                # same way twice; a drifting label makes the published series
                # meaningless.
                "options": {"temperature": 0, "num_predict": max_tokens},
            },
            {},
        )
        reply = (out.get("message") or {}).get("content", "")
        return reply, self._usage(
            prompt, reply, items=0,
            prompt_tokens=out.get("prompt_eval_count", 0),
            completion_tokens=out.get("eval_count", 0),
        )


class OpenAICompatBackend(Backend):
    """Any endpoint that speaks `/chat/completions`."""

    name = "openai-compat"
    unit = "usd"

    def __init__(self, base: str, model: str, key: str,
                 usd_per_mtok_in: float, usd_per_mtok_out: float) -> None:
        self.base = base.rstrip("/")
        self.model = model
        self.key = key
        self.in_rate = usd_per_mtok_in
        self.out_rate = usd_per_mtok_out

    def available(self) -> bool:
        return bool(self.key and self.base)

    def run(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        out = post_json(
            f"{self.base}/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            {"Authorization": f"Bearer {self.key}"},
        )
        reply = out["choices"][0]["message"]["content"]
        usage = out.get("usage") or {}
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        cost = (pt * self.in_rate + ct * self.out_rate) / 1_000_000
        return reply, self._usage(prompt, reply, 0, pt, ct, cost)


class FlopCli:
    """The official Flop CLI, run as a subprocess.

    The network half of a session (opening the channel, checking every turn the
    miner signs, acknowledging it, settling) is what the CLI already does and
    verifies; writing a second client would only add a second place for it to go
    wrong. This class only builds the command line, hands the keystore password
    over the environment, and reads the JSON back.

    The keystore is the agent's own chain account (chain-key.json), encrypted;
    its password is read from chain-key.pass and never printed or logged.
    """

    def __init__(self, network: str = "", package: str = "", keystore: str = "",
                 password_file: str = "", timeout: int = 0, runner=None) -> None:
        self.network = network or os.environ.get("FLOP_NETWORK", "testnet")
        self.package = package or os.environ.get("FLOP_CLI", "@flop-labs/cli@0.8.3")
        self.keystore = Path(keystore or os.environ.get("FLOP_KEYSTORE", "") or HERE / "chain-key.json")
        self.password_file = Path(password_file or os.environ.get("FLOP_KEY_PASSWORD_FILE", "")
                                  or HERE / "chain-key.pass")
        self.timeout = timeout or int(os.environ.get("FLOP_CLI_TIMEOUT", "420"))
        self.runner = runner or subprocess.run

    def ready(self) -> bool:
        return bool(shutil.which("npx")) and self.keystore.exists() and self.password_file.exists()

    def address(self) -> str:
        """The SS58 address inside the keystore. Public data, read without the password."""
        try:
            return json.loads(self.keystore.read_text(encoding="utf-8")).get("address", "")
        except (OSError, json.JSONDecodeError):
            return ""

    def balance(self) -> float | None:
        """Free plus reserved FLOP in the agent's account, or None if unreadable."""
        addr = self.address()
        out = self.call("balance", addr) if addr else {}
        total = str(out.get("total", ""))
        return int(total) / 10**18 if out.get("ok") and total.isdigit() else None

    def genesis_error(self) -> str:
        """Why this network may not be used yet, or "" when it may.

        FLOP_GENESIS is the genesis hash copied from the network's official page.
        Outside local and devnet nothing is claimed or signed until it is set and
        the RPC serves exactly that chain.
        """
        want = os.environ.get("FLOP_GENESIS", "").strip().lower()
        if self.network in ("local", "devnet") and not want:
            return ""
        if not want:
            return f"FLOP_GENESIS is not set, so {self.network} spending is off"
        got = ""
        for wait in (0, *RPC_WAITS):
            # Only silence is worth asking about again; a wrong chain is an answer.
            time.sleep(wait)
            rpc = self.call("network", "status").get("rpc") or {}
            got = str(rpc.get("genesis_hash") or "").lower()
            if got:
                break
        if got != want:
            return f"RPC genesis is {got or 'unreadable'}, expected {want}"
        return ""

    def call(self, *args: str, signed: bool = False) -> dict:
        """Run one CLI command with --json and return its parsed output.

        A non-JSON answer, a timeout, or `"ok": false` all come back as a dict
        with `ok: False` and a short reason, so the caller has one shape to read.
        """
        cmd = [shutil.which("npx") or "npx", "--yes", "--package", self.package, "flop",
               "--network", self.network]
        env = dict(os.environ)
        if signed:
            cmd += ["--keystore", str(self.keystore), "--password-env", "FLOP_KEY_PASSWORD"]
            env["FLOP_KEY_PASSWORD"] = self.password_file.read_text(encoding="utf-8").strip()
        cmd += [*args, "--json"]
        try:
            out = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "TIMEOUT", "message": f"no answer in {self.timeout}s"}
        return parse_cli_json(out.stdout, out.returncode, out.stderr)


def parse_cli_json(stdout: str, returncode: int = 0, stderr: str = "") -> dict:
    """The CLI prints one JSON object, on stdout when it succeeds and on stderr
    when it fails; npm may print warnings around it."""
    # Polkadot's logger prints its own `{...}` fragments into the same stream,
    # so take the first complete object that looks like a CLI result.
    dec = json.JSONDecoder()
    for text in (stdout or "", stderr or ""):
        i = text.find("{")
        while i >= 0:
            try:
                data, _ = dec.raw_decode(text, i)
                if isinstance(data, dict) and ({"ok", "code"} & data.keys() or text is stdout):
                    data.setdefault("ok", returncode == 0)
                    return data
            except json.JSONDecodeError:
                pass
            i = text.find("{", i + 1)
    tail = ((stderr or "") + "\n" + (stdout or "")).strip().splitlines()[-1:] or [""]
    return {"ok": False, "code": "NO_JSON", "message": f"exit {returncode}: {tail[0][:200]}"}


def _first(d: dict, *keys, default=None):
    """The CLI's field names are not frozen; take the first one present, one level deep."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            got = _first(v, *keys, default=None)
            if got is not None:
                return got
    return default


def spent_today(path: Path = LEDGER, day: str = "") -> float:
    """FLOP spent today (UTC), from the ledger. The budget is enforced from the
    same file that is published, so the cap and the record cannot disagree."""
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    total = 0.0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("unit") == "FLOP" and str(row.get("at", "")).startswith(day):
                    total += float(row.get("cost", 0.0))
    except FileNotFoundError:
        pass
    return total


def rows_of(path: Path) -> list[dict]:
    """The readable JSON rows of a log; a half-written line is skipped."""
    rows = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except FileNotFoundError:
        pass
    return rows


def budget_left(network: str, daily_budget: float = 0.0, ledger: Path = LEDGER,
                faucet_log: Path = FAUCET_LOG) -> float:
    """FLOP this agent may still spend on `network`.

    What the faucet granted minus what was already spent there. An unspent
    remainder carries over to the next day, and nothing that did not come from
    the faucet is ever spent. A daily cap, when one is set, limits it further.
    """
    granted = sum(float(r.get("amount") or 0) for r in rows_of(faucet_log)
                  if r.get("ok") and r.get("network") == network)
    spent = sum(float(r.get("cost") or 0) for r in rows_of(ledger)
                if r.get("unit") == "FLOP" and r.get("network") == network)
    left = granted - spent - FEE_RESERVE
    if daily_budget:
        left = min(left, daily_budget - spent_today(ledger))
    return left


def load_miners(path: Path = HERE / "flop-miners.json") -> list[dict]:
    """Miners this agent may open direct sessions with: [{"miner", "endpoint", "model"}].

    Only miners with a reachable, published session endpoint belong here. A
    direct session against a miner that cannot be reached still opens a channel
    and escrows FLOP until it times out, so guessing endpoints is not allowed.
    """
    env = os.environ.get("FLOP_MINERS", "").strip()
    try:
        rows = json.loads(env) if env else json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("miner") and r.get("endpoint")]


class FlopBackend(Backend):
    """Buys inference on the Flop network and records every settled session.

    Only settled compute-channel spend from the agent's own account counts
    toward its allocation (Yellow Paper R8.4), so the direct path is the main
    one: the agent's own wallet opens the channel with a named miner, the CLI
    verifies each signed turn and settles it, and the ledger row carries the
    channel and settlement so it can be checked on chain.

    Spend is capped per call (`max_flop`, the escrow the channel may pay) and by
    what the faucet granted (`budget_left`), plus a daily cap if one is set. When
    that is used up the backend raises BudgetReached instead of buying more.

    If one direct session fails, the backend stops for the rest of the run:
    a failed session can leave escrow locked until the channel times out, and
    retrying against a broken miner would lock more.
    """

    name = "flop"
    unit = "FLOP"

    def __init__(self, model: str = "", cli: FlopCli | None = None,
                 miners: list[dict] | None = None, max_flop: float = 0.0,
                 daily_budget: float = 0.0, ledger: Path = LEDGER,
                 faucet_log: Path = FAUCET_LOG) -> None:
        self.model = model or os.environ.get("FLOP_MODEL", "qwen/qwen3-8b")
        self.cli = cli or FlopCli()
        self.miners = miners if miners is not None else load_miners()
        self.max_flop = max_flop or float(os.environ.get("FLOP_MAX_PER_CALL", "1"))
        self.daily_budget = daily_budget or float(os.environ.get("FLOP_DAILY_BUDGET", "0"))
        self.ledger = ledger
        self.faucet_log = faucet_log
        self._next = 0
        self._broken = ""
        self._checked = False

    def _candidates(self) -> list[dict]:
        own = [m for m in self.miners if (m.get("model") or self.model) == self.model]
        return own or []

    def available(self) -> bool:
        return self.cli.ready() and bool(self._candidates())

    def run(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        if self._broken:
            raise NotImplementedError(f"stopped after a failed session this run: {self._broken}")
        if not self.cli.ready():
            raise NotImplementedError("Flop CLI, chain-key.json or chain-key.pass missing")
        miners = self._candidates()
        if not miners:
            raise NotImplementedError(
                f"no miner with a published session endpoint serves {self.model} "
                "(flop-miners.json is empty)")
        left = budget_left(self.cli.network, self.daily_budget, self.ledger, self.faucet_log)
        if left < MIN_ESCROW:
            raise BudgetReached(f"faucet FLOP used up ({max(left, 0):.3f} left, "
                                f"{spent_today(self.ledger):.3f} spent today)")
        if not self._checked:
            bad = self.cli.genesis_error()
            if bad:
                raise NotImplementedError(bad)
            self._checked = True
        escrow = round(min(self.max_flop, left), 6)
        before = self.cli.balance()

        m = miners[self._next % len(miners)]
        self._next += 1
        out = self.cli.call(
            "session", "run", "--miner", m["miner"], "--model", self.model,
            "--prompt", prompt, "--max-flop", f"{escrow:g}",
            "--max-tokens", str(max_tokens), "--via", "auto", "--endpoint", m["endpoint"],
            signed=True)
        if not out.get("ok", False) or not _first(out, "reply", "content", "output", "text"):
            self._broken = f"{m['miner'][:10]}: {out.get('code', '')} {out.get('message', '')}".strip()
            raise RuntimeError(self._broken)

        reply = str(_first(out, "reply", "content", "output", "text"))
        paid = _first(out, "minerPaid", "paidFlop", "costFlop", "cost_flop", "payable")
        if paid is not None:
            cost, basis = flop_amount(paid), "reported"
        else:
            # The CLI prints no price, so read it off the wallet. If that is not
            # possible the escrow is the only honest figure, and an upper bound.
            after = self.cli.balance() if before is not None else None
            delta = before - after if before is not None and after is not None else 0.0
            cost, basis = ((round(delta, 9), "balance") if 0 < delta <= escrow + FEE_RESERVE
                           else (escrow, "escrow-cap"))
        usage = self._usage(prompt, reply, 0,
                            prompt_tokens=int(_first(out, "prompt_tokens", "promptTokens", default=0) or 0),
                            completion_tokens=int(_first(out, "completion_tokens", "completionTokens",
                                                         default=0) or 0),
                            cost=cost)
        usage.path = "direct"
        usage.network = self.cli.network
        usage.cost_basis = basis
        usage.miner = str(_first(out, "servingMiner", "miner", default=m["miner"]))
        usage.channel = str(_first(out, "channelId", "channel", default=""))
        usage.settlement = str(_first(out, "settle_tx", "settlementTx", "txHash", default=""))
        return reply, usage


def flop_amount(v) -> float:
    """A FLOP figure from the CLI: either a decimal FLOP string or raw 10^-18 units."""
    s = str(v).strip()
    if s.isdigit() and len(s) > 12:
        return int(s) / 10**18
    try:
        return float(s)
    except ValueError:
        return 0.0


class FlopGatewayBackend(OpenAICompatBackend):
    """Flop's hosted OpenAI-compatible gateway: the fallback, not the main path.

    On devnet the gateway's own account opens and settles the channel, so this
    spend lands on chain under the gateway, not under this agent's wallet; whether
    it counts for the agent is an open question (flop-labs/network#133). It is
    kept so the agent can still do its work when no direct miner is reachable.
    """

    name = "flop-gateway"
    unit = "FLOP"

    def __init__(self, model: str = "", key_file: str = "", base: str = "",
                 daily_budget: float = 0.0, ledger: Path = LEDGER,
                 faucet_log: Path = FAUCET_LOG) -> None:
        path = Path(key_file or os.environ.get("FLOP_GATEWAY_KEY_FILE", "")
                    or Path.home() / ".flop" / "devnet-wallet-agent-key.json")
        cred = {}
        try:
            cred = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        super().__init__(base or os.environ.get("FLOP_GATEWAY", "") or cred.get("gateway", "")
                         or "https://api.flop.network",
                         model or cred.get("model") or "qwen/qwen2.5-0.5b-instruct",
                         cred.get("key", ""), 0.0, 0.0)
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.daily_budget = daily_budget or float(os.environ.get("FLOP_DAILY_BUDGET", "0"))
        self.ledger = ledger
        self.faucet_log = faucet_log
        self.network = os.environ.get("FLOP_NETWORK", "testnet")

    def run(self, prompt: str, max_tokens: int) -> tuple[str, Usage]:
        if budget_left(self.network, self.daily_budget, self.ledger, self.faucet_log) < MIN_ESCROW:
            raise BudgetReached("faucet FLOP used up")
        out = post_json(
            f"{self.base}/chat/completions",
            {"model": self.model, "messages": [{"role": "user", "content": prompt}],
             "temperature": 0, "max_tokens": max_tokens},
            {"Authorization": f"Bearer {self.key}"},
            timeout=300,
        )
        reply = out["choices"][0]["message"]["content"] or ""
        u = out.get("usage") or {}
        receipt = out.get("x_flop_receipt") or {}
        usage = self._usage(prompt, reply, 0, u.get("prompt_tokens", 0),
                            u.get("completion_tokens", 0), float(u.get("cost_flop", 0.0)))
        usage.path = "gateway"
        usage.network = self.network
        usage.cost_basis = "reported"
        usage.miner = receipt.get("miner", "")
        usage.channel = receipt.get("channelId", "")
        return reply, usage


def http_json(url: str, payload: dict | None = None, timeout: int = 60) -> tuple[int, dict]:
    """(status, JSON body). Status 0 means no answer came back, so the outcome is unknown."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": "flop-agent/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status, raw = res.status, res.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)[:200]}
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def ss58_pubkey(address: str) -> str:
    """The public key (hex) inside an SS58 address with a one-byte prefix, checksum verified."""
    n = 0
    for ch in address:
        n = n * 58 + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz".index(ch)
    raw = n.to_bytes(35, "big")
    check = hashlib.blake2b(b"SS58PRE" + raw[:33], digest_size=64).digest()[:2]
    if check != raw[33:]:
        raise ValueError("bad SS58 checksum")
    return raw[1:33].hex()


def sign_claim(message: str, address: str, seed_file: Path) -> str:
    """sr25519 signature over the raw UTF-8 message, by the key that owns `address`."""
    import flopwire  # needs sr25519; only a testnet claim does

    seed = bytes.fromhex(seed_file.read_text(encoding="utf-8").strip().removeprefix("0x"))
    pair = flopwire.keypair_from_seed(seed)
    if pair[0].hex() != ss58_pubkey(address):
        raise ValueError("chain-seed.txt is not the key of this address")
    return "0x" + flopwire.sign(pair, message.encode("utf-8")).hex()


def signed_claim(cli: FlopCli, log: Path, now: float, http=http_json, sign=sign_claim) -> dict:
    """One signed agent claim on the testnet faucet (flop.network/llms.txt, "Get Testnet FLOP").

    The nonce is written down before it is sent and reused until the claim is
    final or refused for good: the faucet answers a repeated nonce with the same
    transaction and never pays it twice, so an interrupted run cannot double claim.
    A grant counts only once the faucet reports it finalized.
    """
    base = os.environ.get("FLOP_FAUCET", TESTNET_FAUCET).rstrip("/")
    addr = cli.address()
    want = os.environ.get("FLOP_GENESIS", "").strip().lower()
    mine = [r for r in rows_of(log) if r.get("network") == cli.network and r.get("nonce")]
    last = mine[-1] if mine else {}
    if now < float(last.get("not_before") or 0):
        return {"ok": True, "skipped": True,
                "next_in_h": round((float(last["not_before"]) - now) / 3600, 1)}

    status, chain = http(f"{base}/chain")
    if chain.get("network") != cli.network or str(chain.get("genesisHash", "")).lower() != want:
        return {"ok": False, "code": "FAUCET_CHAIN", "message": f"faucet serves another chain or none (HTTP {status})"}
    _, limits = http(f"{base}/limits")
    lane = limits.get("faucet") or {}
    if lane.get("status") != "open" or (lane.get("agent") or {}).get("claimPath") != CLAIM_PATH:
        return {"ok": False, "code": "FAUCET_CLOSED", "message": f"faucet status {lane.get('status')}"}

    def write(**kw) -> None:
        row = {"t": now, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
               "network": cli.network, "address": addr, "nonce": nonce, "ok": False, "amount": 0.0, **kw}
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    nonce = str(last["nonce"]) if last.get("open") else str(int(now * 1000))
    try:
        sig = sign(f"flop-faucet-agent/v1|{want}|{addr}|{nonce}", addr, cli.keystore.parent / "chain-seed.txt")
    except (OSError, ValueError, ImportError) as e:
        return {"ok": False, "code": "SIGN", "message": f"{type(e).__name__}: {e}"}
    write(code="SENDING", open=True)
    body = {"address": addr, "nonce": nonce, "signature": sig}
    for wait in (0, *FINAL_WAITS):
        time.sleep(wait)
        status, out = http(base + CLAIM_PATH, body)
        if not (status == 200 and out.get("success") and out.get("status") == "included"):
            break

    tx = str(out.get("txHash") or "")
    if status == 200 and out.get("success"):
        final = out.get("status") == "finalized"
        write(ok=final, open=not final, amount=flop_amount(out.get("amount") or 0) if final else 0.0,
              code="FINALIZED" if final else "PENDING", tx=tx)
        return {"ok": final, "code": "FINALIZED" if final else "PENDING",
                "message": f"{out.get('amount')} FLOP {out.get('status')} {tx}"}
    reason = str(out.get("reason") or out.get("error") or f"HTTP {status}")[:120]
    # Refused for good (fix the request first) closes the nonce. Anything that may
    # still turn out to be a payment keeps it.
    still_open = status in (0, 200, 408, 429) or status >= 500
    wait_s = out.get("retryAfterSeconds")
    write(code=f"HTTP {status}", message=reason, open=still_open, tx=tx,
          not_before=now + float(wait_s) if isinstance(wait_s, (int, float)) else 0)
    return {"ok": False, "code": f"HTTP {status}", "message": reason}


def claim_faucet(cli: FlopCli | None = None, log: Path = FAUCET_LOG,
                 every_hours: float = 0.0) -> dict:
    """Ask the network faucet for this agent's daily test FLOP, at most once per window.

    Devnet goes through the CLI's own `faucet request`. Testnet takes a claim
    signed by the receiving account instead. If the faucet ever puts a human check
    in front of the grant, this stops and says so: that step is for the operator.
    """
    cli = cli or FlopCli()
    every = every_hours or float(os.environ.get("FLOP_FAUCET_EVERY_HOURS", "24"))
    now = time.time()
    # A grant, or an answer that leaves open whether one was made, uses up the
    # window: asking again after an unknown outcome could claim twice.
    last = max((float(r.get("t", 0)) for r in rows_of(log) if r.get("network") == cli.network
                and (r.get("ok") or r.get("code") in ("FAUCET_OUTCOME_UNKNOWN", "TIMEOUT"))),
               default=0.0)
    if now - last < every * 3600:
        return {"ok": True, "skipped": True, "next_in_h": round((every * 3600 - (now - last)) / 3600, 1)}
    bad = cli.genesis_error()
    if bad:
        return {"ok": False, "message": bad}
    addr = cli.address()
    if not addr:
        return {"ok": False, "message": "no address in the keystore"}
    if cli.network == "testnet":
        return signed_claim(cli, log, now)
    out = cli.call("faucet", "request", addr)
    # "funded recently" means the grant for this window already landed: log it
    # as done so the next runs wait out the window instead of asking again.
    if not out.get("ok") and "funded recently" in str(out.get("message", "")).lower():
        out = {**out, "ok": True, "already": True}
    row = {"t": now, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "network": cli.network, "address": addr, "ok": bool(out.get("ok")),
           "amount": 0.0 if out.get("already") else flop_amount(out.get("amount") or 0),
           "code": out.get("code", ""), "message": str(out.get("message", ""))[:200]}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return out


def choose_backend(name: str = "") -> Backend:
    """Pick a backend from the environment, or say plainly that there is none.

    Falling back to a silent no-op would let a run report zero spend as though
    it were a real measurement, so an unavailable backend stays unavailable and
    the caller decides what to say about it.
    """
    name = (name or os.environ.get("AGENT_INFERENCE") or "auto").lower()
    model = os.environ.get("AGENT_MODEL", "llama3.2")

    if name == "flop":
        # Direct sessions from the agent's own wallet when a miner is reachable,
        # the hosted gateway otherwise (only if a gateway key exists).
        direct = FlopBackend()
        if direct.available():
            return direct
        # The gateway is opt-in: its spend is settled under the gateway's account,
        # and an expired key would only fail every run.
        if os.environ.get("FLOP_USE_GATEWAY") == "1":
            gw = FlopGatewayBackend()
            if gw.available():
                return gw
        return direct
    if name in ("ollama", "auto"):
        b = OllamaBackend(os.environ.get("OLLAMA_HOST", "http://localhost:11434"), model)
        if b.available() or name == "ollama":
            return b
    if name in ("openai", "openai-compat", "auto"):
        b = OpenAICompatBackend(
            os.environ.get("AGENT_API_BASE", ""),
            model,
            os.environ.get("AGENT_API_KEY", ""),
            float(os.environ.get("AGENT_USD_PER_MTOK_IN", "0")),
            float(os.environ.get("AGENT_USD_PER_MTOK_OUT", "0")),
        )
        if b.available():
            return b
    return Backend()


def record(usage: Usage, path: Path = LEDGER) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(usage)) + "\n")


def totals(path: Path = LEDGER) -> dict:
    """What this agent has spent on inference, all time."""
    calls = items = pt = ct = flops = 0
    cost = 0.0
    flop_cost = 0.0
    flop_calls = 0
    flop_bounds = 0
    unit = "none"
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A half-written final line after a killed run must not
                    # discard every row before it.
                    continue
                calls += 1
                items += row.get("items", 0)
                pt += row.get("prompt_tokens", 0)
                ct += row.get("completion_tokens", 0)
                flops += row.get("flops", 0)
                cost += row.get("cost", 0.0)
                unit = row.get("unit", unit)
                if row.get("unit") == "FLOP":
                    flop_calls += 1
                    flop_cost += row.get("cost", 0.0)
                    flop_bounds += row.get("cost_basis") == "escrow-cap"
    except FileNotFoundError:
        pass
    return {
        "calls": calls, "items": items, "tokens": pt + ct,
        "flops": flops, "cost": round(cost, 6), "unit": unit,
        # Spend on Flop kept apart: summing FLOP with local or USD rows would be nonsense.
        "flop_calls": flop_calls, "flop_cost": round(flop_cost, 6),
        # Rows whose price is only an upper bound; if any, the total is one too.
        "flop_bounds": flop_bounds,
    }
