#!/usr/bin/env python3
"""Local web UI for minting OpenSea SeaDrop drops.

What this adds on top of the external CLI
--------------------------------
* Auto chain detection. OpenSea's `collectionBySlug` metadata query is
  UNAUTHENTICATED and returns `chain { identifier networkId }`, so we can learn
  which chain a drop lives on before launching anything, then point RPC_URL at
  the matching network. The CLI itself cannot switch chains - it uses whatever
  RPC_URL resolves to - so the UI picks the RPC for it.
* Auto price. The per-stage `eligiblePrice` field is UNAUTHORIZED without a SIWE
  session, so instead we read the price straight off the SeaDrop contract with
  `getPublicDrop(address)`. That is on-chain truth and needs no key. It covers
  public-sale stages; allowlist/presale prices stay unknown.
* One-shot mint. A background thread drives the external CLI's interactive prompts end to
  end, so the browser just polls job state.

Key handling: the CLI reads WALLET_KEY only from .env, and only once at start.
So the key is written, the CLI is launched, and as soon as it logs
"Config loaded:" the key is shredded back out. Nothing persists between runs.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from eth_hash.auto import keccak
from eth_keys import keys
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import turbo

# All paths are overridable so the service is not tied to one machine's layout.
# WORKDIR matters: the external mint CLI resolves .env by walking up from the current
# directory, so jobs must run with an explicit cwd that contains it.
BIN = os.environ.get("OSNM_BIN", "/usr/local/bin/opensea-mint")
WORKDIR = Path(os.environ.get("OSNM_WORKDIR", Path.home() / "mint-cli"))
ENV_PATH = WORKDIR / ".env"
BASE = Path(os.environ.get("OSNM_STATE_DIR", Path.home() / ".osnm-ui"))
JOBS_DIR = BASE / "jobs"
NETWORKS_PATH = BASE / "networks.json"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

GQL_URL = "https://gql.opensea.io/graphql"
APP_ID = "os2-web"
SEADROP = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"

KNOWN_SETTINGS = [
    "WALLET_KEY", "WALLETS_FILE", "SPONSORED", "RECIPIENT_ADDRESS",
    "SPONSOR_KEY", "SPONSORED_EXECUTOR_ADDRESS",
    "SPONSORED_OPERATION_DEADLINE_SECONDS", "RPC_URL", "FEE_AUTOMATIC",
    "MAX_FEE_PER_GAS_GWEI", "MAX_PRIORITY_FEE_PER_GAS_GWEI",
    "REPLACEMENT_BUMP_BPS", "GAS_LIMIT", "SCHEDULE_REFRESH_INTERVAL_SECONDS",
    "TRANSACTION_MAX_ATTEMPTS", "PENDING_TIMEOUT_SECONDS",
    "RECEIPT_POLL_BASE_DELAY_MS", "RECEIPT_POLL_MAX_DELAY_MS",
    "OPENSEA_REQUEST_TIMEOUT_MS", "ELIGIBILITY_REQUEST_TIMEOUT_MS",
    "OPENSEA_MAX_ATTEMPTS", "OPENSEA_RETRY_INTERVAL_MS",
    "OPENSEA_CALLDATA_MAX_ATTEMPTS",
]

# Defaults verified reachable from this host. Public endpoints are fine for
# resolving and balance checks, but replace them with a private RPC before a
# contested mint - that is what decides whether you land the transaction.
DEFAULT_NETWORKS = {
    "1":     {"name": "Ethereum",     "symbol": "ETH", "rpc": "https://ethereum-rpc.publicnode.com"},
    "8453":  {"name": "Base",         "symbol": "ETH", "rpc": "https://mainnet.base.org"},
    "42161": {"name": "Arbitrum One", "symbol": "ETH", "rpc": "https://arb1.arbitrum.io/rpc"},
    "10":    {"name": "Optimism",     "symbol": "ETH", "rpc": "https://mainnet.optimism.io"},
    "137":   {"name": "Polygon",      "symbol": "POL", "rpc": "https://polygon-bor-rpc.publicnode.com"},
    "4663":  {"name": "Robinhood",    "symbol": "ETH", "rpc": ""},
    "57073": {"name": "Ink",          "symbol": "ETH", "rpc": "https://rpc-gel.inkonchain.com"},
    # Arc pays gas in USDC, but the native balance still uses 18 decimals
    # (verified against real on-chain transaction values), so no special casing.
    "5042":  {"name": "Arc",          "symbol": "USDC", "rpc": "https://rpc.arc-scan.org"},
}

# Alchemy's per-network subdomains. Every one of these was probed and returns
# HTTP 401 for a bad key, which confirms the host exists and the path is right.
ALCHEMY_HOSTS = {
    "1": "eth-mainnet", "10": "opt-mainnet", "137": "polygon-mainnet",
    "8453": "base-mainnet", "42161": "arb-mainnet", "4663": "robinhood-mainnet",
    "57073": "ink-mainnet", "5042": "arc-mainnet",
}

ANSI = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
RE_PHASE = re.compile(
    r"^\s*(\d+)\.\s+Stage\s+(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
    r"([^|]+?)\s*\|\s*(available|schedulable|unavailable)\s*$"
)
RE_TXHASH = re.compile(r"\b(0x[0-9a-fA-F]{64})\b")

P_TARGET, P_PHASES = "Mint target: ", "Phases: "
P_TOKEN, P_QTY, P_ANSWER = "Token ID: ", "Quantity: ", "Answer [y/N]: "
CONFIG_LOADED = "Config loaded:"

COLLECTION_QUERY = """
query MintCollectionMetadata($slug: String!) {
  collectionBySlug(slug: $slug) {
    __typename
    ... on Collection {
      slug address chain { identifier networkId }
      drop {
        __typename
        identifier { contractAddress chain { identifier } }
        stages {
          __typename stageType stageIndex startTime endTime
          maxTotalMintableByWallet
          ... on Erc1155SeaDropV2Stage {
            fromTokenId toTokenId maxTotalMintableByWalletPerToken
          }
        }
      }
    }
  }
}
"""
SEARCH_QUERY = """
query MintCollectionSearch($query: String!) {
  collectionsByQuery(query: $query, limit: 50) {
    __typename slug address chain { identifier networkId }
  }
}
"""

app = FastAPI(title="SeaDrop Mint Console")
_env_lock = threading.Lock()
JOBS: dict[str, "Job"] = {}


def strip_ansi(t: str) -> str:
    return ANSI.sub("", t)


def normalise_key(raw: str) -> str:
    k = re.sub(r"\s+", "", raw or "")   # tolerate pasted whitespace
    k = k[2:] if k.lower().startswith("0x") else k
    if not re.fullmatch(r"[0-9a-fA-F]{64}", k):
        bad = "".join(sorted({c for c in k if c not in "0123456789abcdefABCDEF"}))
        detail = f"private key must be 64 hex characters - got {len(k)}"
        if bad:
            detail += f", including non-hex character(s): {bad!r}"
        raise HTTPException(400, detail)
    if int(k, 16) == 0:
        raise HTTPException(400, "private key cannot be zero")
    return "0x" + k.lower()


def address_of(key: str) -> str:
    return keys.PrivateKey(bytes.fromhex(key[2:])).public_key.to_checksum_address()


# --------------------------------------------------------------------------
# networks + .env
# --------------------------------------------------------------------------
def load_networks() -> dict[str, dict]:
    nets = dict(DEFAULT_NETWORKS)
    if NETWORKS_PATH.exists():
        try:
            saved = json.loads(NETWORKS_PATH.read_text(encoding="utf-8"))
            for cid, cfg in saved.items():
                nets.setdefault(cid, {"name": f"chain {cid}", "symbol": ""})
                nets[cid] = {**nets[cid], **cfg}
        except (OSError, ValueError):
            pass
    else:
        # Seed Robinhood's RPC from whatever .env already had.
        current = read_env().get("RPC_URL", "")
        if current:
            nets["4663"]["rpc"] = current
    return nets


def save_networks(nets: dict[str, dict]) -> None:
    NETWORKS_PATH.write_text(json.dumps(nets, indent=2), encoding="utf-8")
    os.chmod(NETWORKS_PATH, 0o600)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            n, _, v = line.partition("=")
            values[n.strip()] = v.strip()
    return values


def _write_env(values: dict[str, str]) -> None:
    unknown = [k for k in values if k not in KNOWN_SETTINGS]
    if unknown:
        raise HTTPException(400, f"refusing to write unknown setting(s): {unknown}")
    if values.get("FEE_AUTOMATIC", "").lower() == "true":
        values.pop("MAX_FEE_PER_GAS_GWEI", None)
        values.pop("MAX_PRIORITY_FEE_PER_GAS_GWEI", None)
    body = ["# Managed by the SeaDrop mint console. Single-wallet mode only.",
            "# WALLET_KEY exists here only for the instant a mint starts.", ""]
    body += [f"{k}={values[k]}" for k in KNOWN_SETTINGS if k in values]
    tmp = ENV_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as h:
        h.write("\n".join(body) + "\n")
        h.flush()
        os.fsync(h.fileno())
    os.replace(tmp, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)


def update_env(updates: dict[str, str]) -> None:
    v = read_env()
    v.update(updates)
    _write_env(v)


def scrub_key() -> None:
    if not ENV_PATH.exists():
        return
    v = read_env()
    if "WALLET_KEY" not in v:
        return
    v.pop("WALLET_KEY")
    try:
        size = ENV_PATH.stat().st_size
        with open(ENV_PATH, "r+b") as h:
            for _ in range(2):
                h.seek(0)
                h.write(os.urandom(size))
                h.flush()
                os.fsync(h.fileno())
    except OSError:
        pass
    _write_env(v)


# --------------------------------------------------------------------------
# OpenSea (unauthenticated) + on-chain reads
# --------------------------------------------------------------------------
def gql(op: str, query: str, variables: dict, slug: str) -> dict:
    r = httpx.post(
        GQL_URL,
        json={"operationName": op, "query": query, "variables": variables},
        headers={"Accept": "application/json", "x-app-id": APP_ID,
                 "Origin": "https://opensea.io",
                 "Referer": f"https://opensea.io/collection/{slug}",
                 "User-Agent": "seadrop-console/1.0"},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def parse_locator(text: str) -> tuple[str, str]:
    """Return (kind, value) where kind is 'slug' or 'address'."""
    t = text.strip()
    if t.startswith("0x") and len(t) == 42:
        return "address", t
    if t.startswith("http"):
        m = re.search(r"opensea\.io/(?:[a-z-]+/)?collection/([^/?#]+)", t)
        if m:
            return "slug", m.group(1)
        m = re.search(r"opensea\.io/(?:assets/)?[a-z_]+/(0x[0-9a-fA-F]{40})", t)
        if m:
            return "address", m.group(1)
        raise HTTPException(400, "could not find a collection slug in that URL")
    return "slug", t


def resolve_collection(locator: str) -> dict:
    kind, value = parse_locator(locator)
    slug = value
    if kind == "address":
        data = gql("MintCollectionSearch", SEARCH_QUERY, {"query": value}, value)
        hits = [c for c in (data.get("data") or {}).get("collectionsByQuery") or []
                if (c.get("address") or "").lower() == value.lower()]
        if not hits:
            raise HTTPException(404, f"no OpenSea collection found for {value}")
        # Prefer a hit whose slug is not just the bare address.
        hits.sort(key=lambda c: (c.get("slug", "").lower() == value.lower()))
        slug = hits[0]["slug"]

    data = gql("MintCollectionMetadata", COLLECTION_QUERY, {"slug": slug}, slug)
    col = (data.get("data") or {}).get("collectionBySlug")
    if not col:
        errs = data.get("errors")
        raise HTTPException(404, f"collection '{slug}' not found"
                                 + (f": {errs[0].get('message')}" if errs else ""))
    chain = col.get("chain") or {}
    drop = col.get("drop") or {}
    stages = []
    for s in drop.get("stages") or []:
        stages.append({
            "stage_index": s.get("stageIndex"),
            "stage_type": s.get("stageType"),
            "kind": s.get("__typename"),
            "start_time": s.get("startTime"),
            "end_time": s.get("endTime"),
            "max_per_wallet": s.get("maxTotalMintableByWallet"),
        })
    stages.sort(key=lambda s: s.get("start_time") or "")
    return {
        "slug": col.get("slug"),
        "address": col.get("address"),
        "chain_id": chain.get("networkId"),
        "chain_identifier": chain.get("identifier"),
        "drop_kind": drop.get("__typename"),
        "stages": stages,
    }


def rpc_call(url: str, method: str, params: list) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}, timeout=15.0)
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(payload["error"])
    return payload["result"]


def estimate_max_gas_cost(rpc_url: str, gas_limit: int,
                          attempts: int, bump_bps: int) -> int:
    """Approximate the CLI's own worst-case gas reserve.

    The CLI uses an EIP-1559 ceiling (roughly baseFee*2 + tip) and then bumps
    it by REPLACEMENT_BUMP_BPS for each same-nonce replacement it is allowed to
    send. A plain eth_gasPrice underestimates this by ~3x, which made the UI
    report "sufficient" for wallets the CLI would refuse.
    """
    block = rpc_call(rpc_url, "eth_getBlockByNumber", ["latest", False])
    base = int(block.get("baseFeePerGas") or "0x0", 16)
    try:
        tip = int(rpc_call(rpc_url, "eth_maxPriorityFeePerGas", []), 16)
    except Exception:  # noqa: BLE001 - not every node exposes it
        tip = max(int(rpc_call(rpc_url, "eth_gasPrice", []), 16) // 10, 1)
    initial = base * 2 + tip
    if initial <= 0:
        initial = int(rpc_call(rpc_url, "eth_gasPrice", []), 16)
    factor = (bump_bps / 10_000) ** max(attempts - 1, 0)
    # 30% margin. Base fee moves between our sample and the CLI's, so
    # this is deliberately biased to warn EARLY: a false 'insufficient'
    # is just a red button you can still press, while a false
    # 'sufficient' sends you into a mint the CLI will refuse.
    return int(gas_limit * initial * factor * 1.30)


def onchain_public_drop(rpc_url: str, nft: str) -> dict | None:
    """getPublicDrop(address) on the canonical SeaDrop. Static struct -> 6 words."""
    try:
        sel = "0x" + keccak(b"getPublicDrop(address)").hex()[:8]
        data = sel + nft[2:].lower().rjust(64, "0")
        out = rpc_call(rpc_url, "eth_call", [{"to": SEADROP, "data": data}, "latest"])
        if not out or len(out) < 2 + 64 * 6:
            return None
        b = bytes.fromhex(out[2:])
        w = [int.from_bytes(b[i * 32:(i + 1) * 32], "big") for i in range(6)]
        return {
            "mint_price_wei": w[0], "mint_price": w[0] / 1e18,
            "start_time": w[1], "end_time": w[2],
            "max_per_wallet": w[3], "fee_bps": w[4],
            "restrict_fee_recipients": bool(w[5]),
        }
    except Exception:  # noqa: BLE001 - not every drop is a SeaDrop public drop
        return None


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------
class Job:
    def __init__(self, job_id: str, plan: dict, address: str):
        self.id = job_id
        self.plan = plan
        self.address = address
        self.locator = plan.get("slug") or plan.get("locator") or ""
        self.log_path = JOBS_DIR / f"{job_id}.log"
        self.state = "starting"
        self.error: str | None = None
        self.phases: list[dict] = []
        self.summary: list[str] = []
        self.created = time.time()
        self.proc: subprocess.Popen | None = None
        self.log_handle = None
        # How long the log was when we last answered a prompt.
        # wait_for() will not match a prompt until the log grows past
        # this. Without it the loop re-matches the prompt it just
        # answered and sends the same input twice; the extra copy is
        # then consumed by the NEXT prompt, which cancelled the mint.
        self.answered_at = 0

    def launch(self) -> None:
        self.log_handle = open(self.log_path, "wb")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [BIN, "mint"], cwd=str(WORKDIR),
            stdin=subprocess.PIPE, stdout=self.log_handle,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "TERM": "dumb"},
        )

    def clean_log(self) -> str:
        try:
            return strip_ansi(self.log_path.read_text(encoding="utf-8", errors="replace"))
        except FileNotFoundError:
            return ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def wait_contains(self, needle: str, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if needle in self.clean_log():
                return True
            if not self.alive():
                return needle in self.clean_log()
            time.sleep(0.1)
        return False

    def wait_for(self, prompts: list[str], timeout: float = 120.0) -> str | None:
        end = time.time() + timeout
        while time.time() < end:
            t = self.clean_log()
            if len(t) > self.answered_at:
                for p in prompts:
                    if t.endswith(p):
                        return p
            if not self.alive():
                return None
            time.sleep(0.05)
        return None

    def send(self, line: str) -> None:
        if self.proc and self.proc.stdin:
            self.answered_at = len(self.clean_log())
            self.proc.stdin.write((line + "\n").encode())
            self.proc.stdin.flush()

    def parse_phases(self) -> list[dict]:
        out, lines = [], self.clean_log().splitlines()
        for ln in lines:
            m = RE_PHASE.match(ln)
            if m:
                out.append({
                    "number": int(m.group(1)), "stage_index": int(m.group(2)),
                    "stage_type": m.group(3).strip(), "state": m.group(4).strip(),
                    "eligibility": m.group(5).strip(), "availability": m.group(6),
                    "selectable": m.group(6) != "unavailable",
                })
        return out

    def tx_hashes(self) -> list[str]:
        return sorted(set(RE_TXHASH.findall(self.clean_log())))

    def cancel(self) -> None:
        if self.alive() and self.proc:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                time.sleep(1.0)
                if self.alive():
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.state = "cancelled"

    def refresh(self) -> None:
        if self.state == "scheduled" and not self.alive():
            t = self.clean_log()
            self.state = "finished" if "Mint session finished." in t else "failed"
            if self.state == "failed":
                self.error = last_error(t)

    def as_dict(self) -> dict:
        self.refresh()
        return {
            "id": self.id, "state": self.state, "address": self.address,
            "slug": self.plan.get("slug"), "chain_id": self.plan.get("chain_id"),
            "chain_name": self.plan.get("chain_name"),
            "quantity": self.plan.get("quantity"),
            "price": self.plan.get("mint_price"),
            "starts_at": self.plan.get("starts_at"),
            "alive": self.alive(), "error": self.error,
            "tx": self.tx_hashes(), "phases": self.phases,
            "summary": self.summary, "created": self.created,
        }


def last_error(text: str) -> str | None:
    errs = [ln for ln in text.splitlines() if "[ERROR]" in ln]
    if errs:
        return errs[-1].strip()
    for needle in ("Operation cancelled.", "mint setup was cancelled"):
        if needle in text:
            return f"the CLI aborted: {needle}"
    return None


def drive_mint(job: Job, key: str, quantity: int, stage_index: int | None) -> None:
    """Runs in a background thread: arm key, launch, drive prompts, confirm."""
    saw_prompt = False
    try:
        with _env_lock:
            try:
                update_env({"WALLET_KEY": key, "RPC_URL": job.plan["rpc_url"]})
                job.state = "launching"
                job.launch()
                # The `mint` command does NOT log "Config loaded:" - only
                # `doctor` does. But execute() loads .env and builds the signer
                # BEFORE mint() prompts, so the "Mint target: " prompt is proof
                # the key has already been read into memory. Waiting on that
                # keeps the on-disk window to process-startup time (~100ms)
                # instead of a 30s timeout.
                saw_prompt = job.wait_for([P_TARGET], timeout=45) is not None
            finally:
                scrub_key()
        del key
        if "WALLET_KEY" in read_env():
            job.error = "key scrub failed; aborted"
            job.cancel()
            job.state = "failed"
            return

        job.state = "resolving"
        if not saw_prompt:
            job.state = "failed"
            job.error = last_error(job.clean_log()) or "bot never asked for a target"
            return
        job.send(job.plan["locator_sent"])

        prompt = job.wait_for([P_PHASES, P_TOKEN, P_QTY, P_ANSWER], timeout=180)
        job.phases = job.parse_phases()
        if prompt is None:
            job.state = "failed"
            job.error = last_error(job.clean_log()) or "no selectable phase for this wallet"
            return

        if prompt == P_PHASES:
            sel = None
            if stage_index is not None:
                sel = next((p for p in job.phases
                            if p["stage_index"] == stage_index and p["selectable"]), None)
            if sel is None:
                sel = next((p for p in job.phases if p["selectable"]), None)
            if sel is None:
                job.state = "failed"
                job.error = "no selectable phase"
                return
            job.state = "configuring"
            job.send(str(sel["number"]))

        job.state = "configuring"
        for _ in range(6):
            got = job.wait_for([P_TOKEN, P_QTY, P_ANSWER], timeout=120)
            if got is None:
                job.state = "failed"
                job.error = last_error(job.clean_log()) or "prompt timed out"
                return
            if got == P_ANSWER:
                break
            job.send("0" if got == P_TOKEN else str(quantity))

        tail = job.clean_log().split("Answer [y/N]")[0].strip().splitlines()
        job.summary = [ln for ln in tail[-14:] if ln.strip()]
        job.send("y")
        job.state = "scheduled"
        time.sleep(1.5)
    except Exception as exc:  # noqa: BLE001
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        try:
            scrub_key()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Loopback guard
# --------------------------------------------------------------------------
@app.middleware("http")
async def only_loopback(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in {"127.0.0.1", "localhost", "::1", ""}:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class NetworksIn(BaseModel):
    networks: dict[str, dict]


class ResolveIn(BaseModel):
    locator: str
    wallet_key: str | None = None
    quantity: int = 1


class MintIn(BaseModel):
    locator: str
    wallet_key: str
    quantity: int = 1
    chain_id: int
    rpc_url: str
    stage_index: int | None = None


class TurboIn(BaseModel):
    locator: str
    wallet_key: str
    quantity: int = 1
    gas_limit: int | None = None
    base_multiplier: float = 3.0
    tip_gwei: float | None = None
    fire_offset_ms: int = 0
    # Supplying a raw contract plus a chain skips OpenSea entirely, which is
    # what makes turbo usable on chains OpenSea does not index.
    chain_id: int | None = None


class AlchemyIn(BaseModel):
    api_key: str
    replace_existing: bool = True


TURBO_JOBS: dict[str, Any] = {}


def rpc_list_for(chain_id: int) -> list[str]:
    """A chain's RPC field may hold several comma-separated endpoints; the
    turbo path fires at all of them at once."""
    raw = (load_networks().get(str(chain_id), {}).get("rpc") or "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    if not urls:
        raise HTTPException(400, f"no RPC configured for chain {chain_id}")
    return urls


def turbo_context(locator: str, chain_id: int | None = None) -> tuple[dict, list[str]]:
    text = locator.strip()
    if text.lower().startswith("0x") and len(text) == 42 and chain_id:
        # Direct contract mode: no OpenSea call at all. Everything turbo needs
        # comes from the chain, so this works even where OpenSea has no index.
        return ({"address": text, "slug": text[:10] + "…",
                 "chain_id": chain_id, "drop_kind": None},
                rpc_list_for(chain_id))
    info = resolve_collection(locator)
    if not info.get("chain_id"):
        raise HTTPException(404, "could not determine the drop's chain")
    return info, rpc_list_for(info["chain_id"])


@app.post("/api/networks/alchemy")
def set_alchemy(body: AlchemyIn):
    """Fill every supported network with that account's Alchemy endpoint."""
    key = body.api_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,80}", key):
        raise HTTPException(400, "that does not look like an Alchemy API key")
    nets = load_networks()
    applied = []
    for cid, host in ALCHEMY_HOSTS.items():
        if cid not in nets:
            continue
        if not body.replace_existing and nets[cid].get("rpc"):
            continue
        nets[cid]["rpc"] = f"https://{host}.g.alchemy.com/v2/{key}"
        applied.append(nets[cid]["name"])
    save_networks(nets)

    # Report which ones actually answer, so a key without a network enabled on
    # the Alchemy dashboard shows up here instead of failing at mint time.
    checks = {}
    for cid in ALCHEMY_HOSTS:
        if cid not in nets or not nets[cid].get("rpc"):
            continue
        try:
            got = int(rpc_call(nets[cid]["rpc"].split(",")[0], "eth_chainId", []), 16)
            checks[nets[cid]["name"]] = "ok" if got == int(cid) else f"wrong chain {got}"
        except Exception as exc:  # noqa: BLE001
            checks[nets[cid]["name"]] = f"failed: {str(exc)[:80]}"
    return {"applied": applied, "reachable": checks}


@app.post("/api/turbo/plan")
def turbo_plan(body: TurboIn):
    """Read everything off-chain-free: price, fee recipient, allowance, fees.
    Nothing is signed and nothing is sent."""
    key = normalise_key(body.wallet_key)
    wallet = address_of(key)
    del key
    info, rpcs = turbo_context(body.locator, body.chain_id)
    try:
        plan = turbo.build_plan(rpcs, info["address"], wallet, body.quantity,
                                body.gas_limit, body.base_multiplier, body.tip_gwei)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cannot build a public-mint plan: {exc}") from exc
    plan |= {"slug": info["slug"], "wallet": wallet,
             "chain_name": load_networks().get(str(plan["chain_id"]), {})
                           .get("name", f"chain {plan['chain_id']}"),
             "drop_kind": info.get("drop_kind")}
    return plan


@app.post("/api/turbo/arm")
def turbo_arm(body: TurboIn):
    key = normalise_key(body.wallet_key)
    info, rpcs = turbo_context(body.locator, body.chain_id)
    wallet = address_of(key)
    try:
        plan = turbo.build_plan(rpcs, info["address"], wallet, body.quantity,
                                body.gas_limit, body.base_multiplier, body.tip_gwei)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"cannot build a public-mint plan: {exc}") from exc
    if plan["underfunded"]:
        raise HTTPException(400,
                            f"balance {plan['balance']:.9f} is under the worst-case "
                            f"cost {plan['worst_case_cost']:.9f}")
    if plan.get("remaining_allowance") == 0:
        raise HTTPException(400, "this wallet has already used its full mint allowance")
    if plan["is_open"] and plan["simulated_ok"] is False:
        raise HTTPException(400, f"simulation failed: {plan['simulation_error']}")

    job_id = time.strftime("%Y%m%d-%H%M%S") + "-t"
    job = turbo.arm(job_id, plan, key, info["slug"], body.fire_offset_ms)
    del key
    TURBO_JOBS[job_id] = job
    return job.as_dict()


@app.get("/api/turbo/jobs")
def turbo_jobs():
    return [j.as_dict() for j in sorted(TURBO_JOBS.values(),
                                        key=lambda j: j.created, reverse=True)]


@app.get("/api/turbo/jobs/{job_id}")
def turbo_job(job_id: str):
    job = TURBO_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown turbo job")
    return job.as_dict()


@app.post("/api/turbo/jobs/{job_id}/cancel")
def turbo_cancel(job_id: str):
    job = TURBO_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown turbo job")
    job.cancel()
    return job.as_dict()


@app.get("/api/networks")
def get_networks():
    return {"networks": load_networks()}


@app.post("/api/networks")
def set_networks(body: NetworksIn):
    nets = load_networks()
    for cid, cfg in body.networks.items():
        if not str(cid).isdigit():
            raise HTTPException(400, f"bad chain id {cid}")
        rpc = (cfg.get("rpc") or "").strip()
        if rpc and not rpc.startswith(("http://", "https://")):
            raise HTTPException(400, f"RPC for chain {cid} must be http(s)")
        nets.setdefault(str(cid), {"name": f"chain {cid}", "symbol": ""})
        nets[str(cid)]["rpc"] = rpc
    save_networks(nets)
    return {"networks": nets}


@app.get("/api/status")
def status():
    env = read_env()
    return {
        "env_exists": ENV_PATH.exists(),
        "env_mode": oct(ENV_PATH.stat().st_mode & 0o777) if ENV_PATH.exists() else None,
        "key_on_disk": "WALLET_KEY" in env,
        "gas_limit": env.get("GAS_LIMIT"),
        "networks_configured": sum(1 for n in load_networks().values() if n.get("rpc")),
    }


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    """Everything needed for the review card, before anything is signed."""
    info = resolve_collection(body.locator)
    nets = load_networks()
    cid = str(info["chain_id"])
    net = nets.get(cid)
    out: dict[str, Any] = dict(info)
    out["chain_name"] = (net or {}).get("name") or f"chain {cid}"
    out["symbol"] = (net or {}).get("symbol") or ""
    out["rpc_url"] = (net or {}).get("rpc") or ""
    out["known_network"] = bool(net)
    out["locator_sent"] = info["slug"]
    out["mintable"] = bool(info.get("drop_kind") and info.get("stages"))

    if not out["mintable"]:
        out["warning"] = (f"'{info['slug']}' has no mintable drop on OpenSea "
                          "- it looks like a secondary-market collection, "
                          "not an active mint.")
        return out

    if not out["rpc_url"]:
        out["warning"] = (f"No RPC configured for {out['chain_name']} "
                          f"(chain {cid}). Add one below before minting.")
        return out

    # chain sanity: the RPC must actually be that chain
    try:
        actual = int(rpc_call(out["rpc_url"], "eth_chainId", []), 16)
        out["rpc_chain_id"] = actual
        if actual != info["chain_id"]:
            out["warning"] = (f"RPC for {out['chain_name']} reports chain {actual}, "
                              f"but the drop is on {info['chain_id']}.")
            return out
    except Exception as exc:  # noqa: BLE001
        out["warning"] = f"RPC unreachable: {exc}"
        return out

    drop = onchain_public_drop(out["rpc_url"], info["address"])
    if drop:
        out["onchain"] = drop
        out["mint_price"] = drop["mint_price"]
        out["price_source"] = "on-chain SeaDrop getPublicDrop()"
        if drop["start_time"]:
            out["starts_at"] = datetime.fromtimestamp(
                drop["start_time"], timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        out["price_source"] = "unknown (not a SeaDrop public drop)"

    if body.wallet_key:
        key = normalise_key(body.wallet_key)
        addr = address_of(key)
        del key
        out["wallet"] = addr
        try:
            bal = int(rpc_call(out["rpc_url"], "eth_getBalance", [addr, "latest"]), 16)
            env_now = read_env()
            gas_limit = int(env_now.get("GAS_LIMIT", "300000"))
            attempts = int(env_now.get("TRANSACTION_MAX_ATTEMPTS", "3"))
            bump = int(env_now.get("REPLACEMENT_BUMP_BPS", "11250"))
            gas_buffer = estimate_max_gas_cost(out["rpc_url"], gas_limit,
                                               attempts, bump)
            price_wei = (drop or {}).get("mint_price_wei", 0)
            need = price_wei * max(body.quantity, 1) + gas_buffer
            out |= {
                "balance": bal / 1e18,
                "gas_buffer": gas_buffer / 1e18,
                "total_price": price_wei * max(body.quantity, 1) / 1e18,
                "needed": need / 1e18,
                "underfunded": bal < need,
            }
        except Exception as exc:  # noqa: BLE001
            out["balance_error"] = str(exc)
    return out


@app.post("/api/mint")
def mint(body: MintIn):
    key = normalise_key(body.wallet_key)
    address = address_of(key)
    if body.quantity < 1:
        raise HTTPException(400, "quantity must be at least 1")
    if not body.rpc_url.startswith(("http://", "https://")):
        raise HTTPException(400, "a valid RPC URL is required")

    info = resolve_collection(body.locator)
    if info["chain_id"] != body.chain_id:
        raise HTTPException(409, "collection chain changed since it was resolved")
    nets = load_networks()
    plan = {
        **info,
        "chain_name": nets.get(str(body.chain_id), {}).get("name", f"chain {body.chain_id}"),
        "rpc_url": body.rpc_url,
        "locator_sent": info["slug"],
        "quantity": body.quantity,
    }
    drop = onchain_public_drop(body.rpc_url, info["address"])
    if drop:
        plan["mint_price"] = drop["mint_price"]
        if drop["start_time"]:
            plan["starts_at"] = datetime.fromtimestamp(
                drop["start_time"], timezone.utc).isoformat().replace("+00:00", "Z")

    job_id = time.strftime("%Y%m%d-%H%M%S")
    job = Job(job_id, plan, address)
    JOBS[job_id] = job
    threading.Thread(target=drive_mint,
                     args=(job, key, body.quantity, body.stage_index),
                     daemon=True).start()
    del key
    return {"id": job_id, "state": job.state}


@app.get("/api/jobs")
def list_jobs():
    return [j.as_dict() for j in sorted(JOBS.values(),
                                        key=lambda j: j.created, reverse=True)]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, offset: int = 0):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    d = job.as_dict()
    text = job.clean_log()
    d |= {"chunk": text[offset:], "offset": len(text)}
    return d


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    job.cancel()
    return {"id": job.id, "state": job.state}


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
