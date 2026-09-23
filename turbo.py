#!/usr/bin/env python3
"""Turbo path: pre-signed, on-chain SeaDrop public mints.

Why this exists
---------------
The osnm-z bot must ask OpenSea's GraphQL API for the transaction target and
calldata at fire time. Measured from this VPS that call costs ~530 ms even on a
warm HTTP/2 connection (the Cloudflare edge is 1.4 ms away, so it is OpenSea's
backend, not the network). With the 250 ms retry gap on top, the bot lands a
transaction roughly 0.55-1.35 s after a stage opens. That loses a real FCFS race.

For a PUBLIC SeaDrop stage none of that is necessary. The mint call is:

    SeaDrop.mintPublic(address nftContract, address feeRecipient,
                       address minterIfNotPayer, uint256 quantity)  payable

Every argument is knowable in advance from the chain itself, so the whole
transaction can be built AND SIGNED before the stage opens. At T-0 the only
work left is one eth_sendRawTransaction (~11 ms to Alchemy from this box).

This never contacts OpenSea. It only works for public stages - allowlist and
signed-presale stages need OpenSea's signature, so those still go through the
bot.

Key handling: signing happens here, in this process, so the private key lives
in memory for as long as it takes to sign. It is never written to disk and
never returned by any endpoint. That is a real difference from the bot path,
where the key only ever reached the Rust process.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx
from eth_account import Account
from eth_hash.auto import keccak

SEADROP = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"


def _sel(sig: str) -> str:
    return "0x" + keccak(sig.encode()).hex()[:8]


SEL_MINT_PUBLIC = _sel("mintPublic(address,address,address,uint256)")
SEL_PUBLIC_DROP = _sel("getPublicDrop(address)")
SEL_FEE_RECIPIENTS = _sel("getAllowedFeeRecipients(address)")
SEL_MINT_STATS = _sel("getMintStats(address)")
SEL_BALANCE_OF = _sel("balanceOf(address)")
ZERO = "0x" + "0" * 40


def _addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _uint(n: int) -> str:
    return format(n, "x").rjust(64, "0")


def rpc(url: str, method: str, params: list, timeout: float = 12.0) -> Any:
    r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(payload["error"].get("message", str(payload["error"])))
    return payload["result"]


def eth_call(url: str, to: str, data: str, frm: str | None = None,
             value: int | None = None) -> str:
    tx: dict[str, Any] = {"to": to, "data": data}
    if frm:
        tx["from"] = frm
    if value is not None:
        tx["value"] = hex(value)
    return rpc(url, "eth_call", [tx, "latest"])


# --------------------------------------------------------------------------
# on-chain reads
# --------------------------------------------------------------------------
def public_drop(url: str, nft: str) -> dict:
    """PublicDrop is a static struct, so it comes back as 6 flat words."""
    out = eth_call(url, SEADROP, SEL_PUBLIC_DROP + _addr(nft))
    b = bytes.fromhex(out[2:])
    if len(b) < 192:
        raise RuntimeError("not a SeaDrop v1 public drop")
    w = [int.from_bytes(b[i * 32:(i + 1) * 32], "big") for i in range(6)]
    # A contract that was never registered with SeaDrop does not revert - it
    # returns six zero words. Without this guard an all-zero "drop" would look
    # like a free mint that opened at unix 0, and we would fire instantly into
    # a guaranteed revert.
    if w[1] == 0 and w[2] == 0 and w[3] == 0:
        raise RuntimeError(
            "this contract has no SeaDrop public drop configured "
            "(getPublicDrop returned empty) - turbo mode only works for "
            "public SeaDrop stages")
    if w[2] and w[2] < int(time.time()):
        raise RuntimeError("the public stage has already ended")
    return {"mint_price_wei": w[0], "start_time": w[1], "end_time": w[2],
            "max_per_wallet": w[3], "fee_bps": w[4],
            "restrict_fee_recipients": bool(w[5])}


def allowed_fee_recipients(url: str, nft: str) -> list[str]:
    out = eth_call(url, SEADROP, SEL_FEE_RECIPIENTS + _addr(nft))
    b = bytes.fromhex(out[2:])
    if len(b) < 64:
        return []
    count = int.from_bytes(b[32:64], "big")
    return ["0x" + b[64 + i * 32 + 12: 64 + (i + 1) * 32].hex() for i in range(count)]


def minted_so_far(url: str, nft: str, wallet: str) -> int | None:
    """getMintStats is the authoritative per-wallet counter; balanceOf is a
    fallback and can be wrong if the wallet bought or sold on the secondary."""
    try:
        out = eth_call(url, nft, SEL_MINT_STATS + _addr(wallet))
        b = bytes.fromhex(out[2:])
        if len(b) >= 32:
            return int.from_bytes(b[0:32], "big")
    except Exception:  # noqa: BLE001
        pass
    try:
        out = eth_call(url, nft, SEL_BALANCE_OF + _addr(wallet))
        return int(out, 16)
    except Exception:  # noqa: BLE001
        return None


def mint_public_calldata(nft: str, fee_recipient: str, minter: str, qty: int) -> str:
    # minterIfNotPayer = 0 means "the payer is the minter", which is what we want.
    return (SEL_MINT_PUBLIC + _addr(nft) + _addr(fee_recipient)
            + _addr(minter) + _uint(qty))


def fee_suggestion(url: str, base_multiplier: float = 3.0,
                   tip_floor_wei: int = 50_000_000) -> tuple[int, int, int]:
    block = rpc(url, "eth_getBlockByNumber", ["latest", False])
    base = int(block.get("baseFeePerGas") or "0x0", 16)
    try:
        tip = int(rpc(url, "eth_maxPriorityFeePerGas", []), 16)
    except Exception:  # noqa: BLE001
        tip = 0
    tip = max(tip, tip_floor_wei)
    max_fee = int(base * base_multiplier) + tip
    return base, tip, max_fee


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------
@dataclass
class TurboJob:
    id: str
    chain_id: int
    nft: str
    slug: str
    wallet: str
    quantity: int
    price_wei: int
    value_wei: int
    gas_limit: int
    max_fee: int
    tip: int
    nonce: int
    start_time: int
    fire_at: float
    rpcs: list[str]
    raw_tx: str
    tx_hash: str
    state: str = "armed"
    error: str | None = None
    created: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    sent_at: float | None = None
    receipt: dict | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    def say(self, msg: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}")

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": "turbo", "state": self.state,
            "chain_id": self.chain_id, "nft": self.nft, "slug": self.slug,
            "wallet": self.wallet, "quantity": self.quantity,
            "price": self.price_wei / 1e18, "value": self.value_wei / 1e18,
            "gas_limit": self.gas_limit,
            "max_fee_gwei": self.max_fee / 1e9, "tip_gwei": self.tip / 1e9,
            "nonce": self.nonce, "start_time": self.start_time,
            "fire_at": self.fire_at, "tx_hash": self.tx_hash,
            "error": self.error, "log": self.log, "created": self.created,
            "sent_at": self.sent_at, "receipt": self.receipt,
            "seconds_to_fire": max(self.fire_at - time.time(), 0),
        }

    def cancel(self) -> None:
        self.stop.set()
        if self.state in {"armed", "waiting"}:
            self.state = "cancelled"
            self.say("cancelled before firing")


def _broadcast(client: httpx.Client, url: str, raw: str) -> tuple[str, float, str | None]:
    """Send on an ALREADY WARM connection. Creating a client here instead would
    cost a TLS handshake (~120 ms measured) at the one moment it matters."""
    started = time.perf_counter()
    try:
        r = client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": "eth_sendRawTransaction",
                                   "params": [raw]}, timeout=10.0)
        ms = (time.perf_counter() - started) * 1000
        payload = r.json()
        if "error" in payload:
            return url, ms, str(payload["error"].get("message", payload["error"]))[:160]
        return url, ms, None if payload.get("result") else "empty response"
    except Exception as exc:  # noqa: BLE001
        return url, (time.perf_counter() - started) * 1000, str(exc)[:160]


def _warm(clients: dict[str, httpx.Client]) -> None:
    """Open (or re-open) the TLS connection and keep it hot."""
    def ping(item):
        url, client = item
        try:
            client.post(url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": "eth_blockNumber", "params": []},
                        timeout=6.0)
        except Exception:  # noqa: BLE001
            pass
    if clients:
        with ThreadPoolExecutor(max_workers=len(clients)) as pool:
            list(pool.map(ping, clients.items()))


def run_turbo(job: TurboJob) -> None:
    """Sleep until the stage opens, then blast the pre-signed transaction."""
    clients = {u: httpx.Client(timeout=10.0,
                               limits=httpx.Limits(max_keepalive_connections=4,
                                                   keepalive_expiry=120.0))
               for u in job.rpcs}
    try:
        job.state = "waiting"
        job.say(f"armed: {job.tx_hash} pre-signed, {len(job.rpcs)} endpoint(s), "
                f"firing at unix {job.fire_at:.3f}")

        _warm(clients)
        job.say("connections warmed")

        # Coarse sleep, then spin the last 40 ms so timer granularity does not
        # cost us the race. Re-warm periodically, and once just before firing,
        # so the socket is certainly hot when it counts.
        last_warm = time.time()
        final_warmed = False
        while not job.stop.is_set():
            remaining = job.fire_at - time.time()
            if remaining <= 0:
                break
            if not final_warmed and remaining <= 1.5:
                _warm(clients)
                final_warmed = True
            elif remaining > 3 and time.time() - last_warm > 20:
                _warm(clients)
                last_warm = time.time()
            time.sleep(min(remaining - 0.04, 0.25) if remaining > 0.08 else 0.0)
        if job.stop.is_set():
            return

        job.state = "firing"
        job.say(f"fired {(time.time() - job.fire_at) * 1000:+.1f} ms vs target")

        with ThreadPoolExecutor(max_workers=max(len(job.rpcs), 1)) as pool:
            results = list(pool.map(
                lambda u: _broadcast(clients[u], u, job.raw_tx), job.rpcs))
        job.sent_at = time.time()
        accepted = 0
        for url, ms, err in results:
            host = url.split("/")[2] if url.count("/") >= 2 else url
            if err:
                job.say(f"  {host}: rejected after {ms:.0f} ms - {err}")
            else:
                accepted += 1
                job.say(f"  {host}: accepted in {ms:.0f} ms")
        if not accepted:
            job.state = "failed"
            job.error = "every RPC rejected the transaction"
            return

        job.state = "sent"
        job.say(f"in mempool as {job.tx_hash}; waiting for a receipt")

        deadline = time.time() + 180
        while time.time() < deadline and not job.stop.is_set():
            try:
                r = rpc(job.rpcs[0], "eth_getTransactionReceipt", [job.tx_hash])
            except Exception:  # noqa: BLE001
                r = None
            if r:
                ok = int(r.get("status", "0x0"), 16) == 1
                job.receipt = {"block": int(r.get("blockNumber", "0x0"), 16),
                               "gas_used": int(r.get("gasUsed", "0x0"), 16),
                               "status": ok}
                job.state = "minted" if ok else "reverted"
                took = time.time() - (job.sent_at or time.time())
                job.say(f"{'MINTED' if ok else 'REVERTED'} in block "
                        f"{job.receipt['block']} after {took:.2f}s, "
                        f"gas used {job.receipt['gas_used']}")
                if not ok:
                    job.error = "the transaction reverted on-chain"
                return
            time.sleep(0.4)
        job.state = "pending"
        job.error = "no receipt within 180s; check the hash on an explorer"
    except Exception as exc:  # noqa: BLE001
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say(f"error: {job.error}")
    finally:
        for client in clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------
# planning + arming
# --------------------------------------------------------------------------
def build_plan(rpc_urls: list[str], nft: str, wallet: str, quantity: int,
               gas_limit: int | None, base_multiplier: float,
               tip_gwei: float | None) -> dict:
    url = rpc_urls[0]
    chain_id = int(rpc(url, "eth_chainId", []), 16)
    drop = public_drop(url, nft)
    recipients = allowed_fee_recipients(url, nft)
    fee_recipient = recipients[0] if recipients else ZERO
    if drop["restrict_fee_recipients"] and not recipients:
        raise RuntimeError("this drop restricts fee recipients but exposes none")

    already = minted_so_far(url, nft, wallet)
    remaining = None
    if already is not None and drop["max_per_wallet"]:
        remaining = max(drop["max_per_wallet"] - already, 0)

    value = drop["mint_price_wei"] * quantity
    data = mint_public_calldata(nft, fee_recipient, ZERO, quantity)
    base, auto_tip, _ = fee_suggestion(url, base_multiplier)
    tip = int(tip_gwei * 1e9) if tip_gwei is not None else auto_tip
    max_fee = int(base * base_multiplier) + tip

    now = int(time.time())
    is_open = drop["start_time"] <= now < (drop["end_time"] or 2 ** 63)

    # Simulating before the stage opens tells us nothing - it reverts by
    # definition - so only do it when the stage is live.
    sim_ok, sim_err = None, None
    if is_open:
        try:
            eth_call(url, SEADROP, data, frm=wallet, value=value)
            sim_ok = True
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            # A poor wallet is a funding problem, not a broken transaction.
            # Reporting it as a failed simulation would be misleading, since
            # the underfunded flag already covers it.
            if "insufficient funds" in text.lower():
                sim_ok, sim_err = None, "inconclusive: wallet cannot cover the value yet"
            else:
                sim_ok, sim_err = False, text[:200]

    if gas_limit is None:
        gas_limit = 300_000
        if is_open:
            try:
                estimated = int(rpc(url, "eth_estimateGas", [{
                    "from": wallet, "to": SEADROP, "data": data,
                    "value": hex(value)}]), 16)
                gas_limit = int(estimated * 1.6)
            except Exception:  # noqa: BLE001
                pass

    balance = int(rpc(url, "eth_getBalance", [wallet, "latest"]), 16)
    worst_cost = value + gas_limit * max_fee

    return {
        "chain_id": chain_id, "nft": nft, "fee_recipient": fee_recipient,
        "quantity": quantity, "price_wei": drop["mint_price_wei"],
        "price": drop["mint_price_wei"] / 1e18,
        "value_wei": value, "value": value / 1e18,
        "start_time": drop["start_time"], "end_time": drop["end_time"],
        "max_per_wallet": drop["max_per_wallet"],
        "already_minted": already, "remaining_allowance": remaining,
        "fee_bps": drop["fee_bps"],
        "restrict_fee_recipients": drop["restrict_fee_recipients"],
        "is_open": is_open, "simulated_ok": sim_ok, "simulation_error": sim_err,
        "gas_limit": gas_limit, "base_fee_gwei": base / 1e9,
        "tip_gwei": tip / 1e9, "max_fee_gwei": max_fee / 1e9,
        "max_fee": max_fee, "tip": tip,
        "balance": balance / 1e18,
        "worst_case_cost": worst_cost / 1e18,
        "underfunded": balance < worst_cost,
        "calldata": data, "calldata_bytes": len(data[2:]) // 2,
        "rpcs": rpc_urls,
    }


def arm(job_id: str, plan: dict, private_key: str, slug: str,
        fire_offset_ms: int) -> TurboJob:
    """Sign now, fire later. The key is used here and then dropped."""
    account = Account.from_key(private_key)
    url = plan["rpcs"][0]
    nonce = int(rpc(url, "eth_getTransactionCount", [account.address, "pending"]), 16)
    tx = {
        "chainId": plan["chain_id"], "nonce": nonce, "to": SEADROP,
        "value": plan["value_wei"], "gas": plan["gas_limit"],
        "maxFeePerGas": plan["max_fee"], "maxPriorityFeePerGas": plan["tip"],
        "data": plan["calldata"], "type": 2,
    }
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None)
    if raw is None:
        raw = signed.rawTransaction
    raw_hex = "0x" + raw.hex().removeprefix("0x")
    digest = getattr(signed, "hash", None)
    tx_hash = "0x" + digest.hex().removeprefix("0x")

    fire_at = max(plan["start_time"] + fire_offset_ms / 1000.0, time.time())
    job = TurboJob(
        id=job_id, chain_id=plan["chain_id"], nft=plan["nft"], slug=slug,
        wallet=account.address, quantity=plan["quantity"],
        price_wei=plan["price_wei"], value_wei=plan["value_wei"],
        gas_limit=plan["gas_limit"], max_fee=plan["max_fee"], tip=plan["tip"],
        nonce=nonce, start_time=plan["start_time"], fire_at=fire_at,
        rpcs=list(plan["rpcs"]), raw_tx=raw_hex, tx_hash=tx_hash,
    )
    job.say(f"signed nonce {nonce}, value {plan['value']}, gas {plan['gas_limit']} "
            f"@ max {plan['max_fee_gwei']:.4f} gwei / tip {plan['tip_gwei']:.4f} gwei")
    threading.Thread(target=run_turbo, args=(job,), daemon=True).start()
    return job
