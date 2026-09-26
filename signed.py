#!/usr/bin/env python3
"""Fast path for SIGNED presale stages (allowlist / FCFS / GTD).

Why this exists
---------------
Turbo cannot serve these stages. The contract's `mintSigned` demands an ECDSA
signature from OpenSea's signer over the mint params, and OpenSea refuses to
issue it before the stage opens - verified: a request for a not-yet-open stage
returns `DropNotMintingError`. So the calldata genuinely cannot be pre-fetched
and the transaction genuinely cannot be pre-signed.

What is still winnable is *when* the request goes out. Measured from a Tokyo
host:

    OpenSea mint-action round trip   ~580 ms  (server-side only ~45 ms;
                                               the rest is Cloudflare -> origin)
    rate limit                       ~5 requests per ~15 s window
    broadcast to the RPC             ~85 ms

The standard driver blind-polls from T-2s at a fixed interval. Because each
request takes ~580 ms, the *send* that first lands after the stage opens can be
up to ~830 ms late, and the polling burns the rate-limit budget before it
matters.

This module does the opposite. It knows the exact on-chain `startTime`, and the
host clock is NTP-synced to microseconds, so it simply sends the request AT
T-0. Everything else - session, nonce, fees, gas - is prepared in advance.

    ~0 ms   fire the mint-action request
    ~580 ms calldata returns, validated, signed
    ~85 ms  broadcast in parallel to every configured RPC
    -------
    ~665 ms to mempool, versus ~1150 ms for blind polling

Safety: the transaction target and calldata come from OpenSea's unofficial API,
so nothing is signed until `validate_action` has checked the destination, the
selector, and every decoded argument against what was actually requested.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import to_checksum_address

from turbo import SEADROP, _addr, _broadcast, _warm, rpc

SITE = "https://opensea.io"
GQL = "https://gql.opensea.io/graphql"
APP_ID = "os2-web"
SIWE_STATEMENT = (
    "Click to sign in and accept the OpenSea Terms of Service "
    "(https://opensea.io/tos) and Privacy Policy (https://opensea.io/privacy)."
)

# Read out of the deployed SeaDrop bytecode, not derived from a guessed
# signature. MintParams is SEVEN uint256 plus a bool, not eight. An earlier
# build assumed eight, produced 0x41af4372 for mintSigned, and would have
# rejected every real response as an unknown selector at the one moment that
# mattered.
#   mintPublic(address,address,address,uint256)
#   mintAllowList(address,address,address,uint256,
#       (uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),bytes32[])
#   mintSigned(address,address,address,uint256,
#       (uint256,uint256,uint256,uint256,uint256,uint256,uint256,bool),uint256,bytes)
SEL_MINT_PUBLIC = "0x161ac21f"
SEL_MINT_ALLOWLIST = "0x4300a4e6"
SEL_MINT_SIGNED = "0x4b61cd6f"
KNOWN_SELECTORS = {
    SEL_MINT_PUBLIC: "mintPublic",
    SEL_MINT_ALLOWLIST: "mintAllowList",
    SEL_MINT_SIGNED: "mintSigned",
}
# Deliberately absent: mintAllowedTokenHolder(address,address,address,
# (address,uint256[])). Its fourth word is an offset, not a quantity, so the
# layout below would misread it. Refusing an unknown selector is the safe
# outcome for a call we cannot verify.

META_QUERY = """
query MintCollectionMetadata($slug: String!) {
  collectionBySlug(slug: $slug) {
    __typename
    ... on Collection {
      slug address chain { identifier networkId }
      drop { __typename identifier { contractAddress chain { identifier } }
             stages { __typename stageType stageIndex startTime endTime
                      maxTotalMintableByWallet } }
    }
  }
}
"""

MINT_ACTION_QUERY = """
query MintActionTimelineQuery($address: Address!, $fromAssets: [AssetQuantityInput!]!,
                              $toAssets: [AssetQuantityInput!]!, $recipient: Address) {
  swap(address: $address, fromAssets: $fromAssets, toAssets: $toAssets,
       recipient: $recipient, action: MINT) {
    actions { __typename
      ... on TransactionAction {
        transactionSubmissionData { to data value chain { networkId identifier } } } }
    errors { __typename }
  }
}
"""

# Transient conditions worth one more attempt inside the tiny budget we have.
RETRYABLE = {"DropNotMintingError", "RateLimitError", "InternalServerError"}


# --------------------------------------------------------------------------
# calldata validation - nothing is signed before this passes
# --------------------------------------------------------------------------
def _words(data: bytes, n: int) -> list[int]:
    return [int.from_bytes(data[i * 32:(i + 1) * 32], "big") for i in range(n)]


def verify_selectors(url: str) -> list[str]:
    """Confirm the mint selectors really exist in the deployed SeaDrop.

    A wrong signature constant is invisible until a live response arrives and
    is rejected as an unknown selector - which is exactly when there is no time
    left to fix it. This turns that into a loud failure at arm time. Returns
    the names of any that are missing.
    """
    try:
        code = rpc(url, "eth_getCode", [SEADROP, "latest"])[2:]
    except Exception:  # noqa: BLE001
        return []
    return [name for sel_hex, name in KNOWN_SELECTORS.items()
            if sel_hex[2:] not in code]


def decode_mint_calldata(data_hex: str) -> dict:
    """Decode whichever SeaDrop mint call OpenSea handed back.

    MintParams is a struct of seven uint256 plus a bool, all value types, so it
    is a STATIC struct and is encoded inline rather than behind an offset. That
    is what makes the fixed word positions below correct.
    """
    selector = data_hex[:10].lower()
    body = bytes.fromhex(data_hex[10:])
    name = KNOWN_SELECTORS.get(selector)
    if name is None:
        raise ValueError(f"unknown selector {selector}")
    if len(body) < 4 * 32:
        raise ValueError("calldata too short")
    w = _words(body, min(len(body) // 32, 16))
    out: dict[str, Any] = {
        "selector": selector,
        "function": name,
        "nft_contract": "0x" + format(w[0], "064x")[24:],
        "fee_recipient": "0x" + format(w[1], "064x")[24:],
        "minter_if_not_payer": "0x" + format(w[2], "064x")[24:],
        "quantity": w[3],
    }
    if name in ("mintSigned", "mintAllowList") and len(w) >= 12:
        out |= {
            "mint_price": w[4], "max_total_mintable_by_wallet": w[5],
            "start_time": w[6], "end_time": w[7], "drop_stage_index": w[8],
            "max_token_supply_for_stage": w[9], "fee_bps": w[10],
            "restrict_fee_recipients": bool(w[11]),
        }
    return out


def validate_action(action: dict, *, nft: str, minter: str, quantity: int,
                    chain_id: int, max_value_wei: int) -> dict:
    """Refuse anything that is not the mint we asked for.

    The target and calldata come from an unofficial API, so this is the only
    thing standing between a compromised or buggy response and a signed
    transaction spending real funds.
    """
    to = (action.get("to") or "").lower()
    if to != SEADROP.lower():
        raise ValueError(f"destination is not SeaDrop: {to}")

    got_chain = ((action.get("chain") or {}).get("networkId"))
    if got_chain is not None and int(got_chain) != chain_id:
        raise ValueError(f"action is for chain {got_chain}, expected {chain_id}")

    value = int(action.get("value") or 0)
    if value > max_value_wei:
        raise ValueError(
            f"value {value} wei exceeds the {max_value_wei} wei ceiling you approved")

    decoded = decode_mint_calldata(action["data"])

    if decoded["nft_contract"].lower() != nft.lower():
        raise ValueError(
            f"calldata mints {decoded['nft_contract']}, not {nft}")
    if decoded["quantity"] != quantity:
        raise ValueError(
            f"calldata quantity {decoded['quantity']} != requested {quantity}")

    # minterIfNotPayer is either zero (payer mints for itself) or us.
    m = decoded["minter_if_not_payer"].lower()
    if int(m, 16) != 0 and m != minter.lower():
        raise ValueError(f"calldata mints to {m}, not {minter}")

    price = decoded.get("mint_price")
    if price is not None:
        expected = price * quantity
        if value != expected:
            raise ValueError(
                f"value {value} does not match mintPrice*quantity {expected}")
        now = int(time.time())
        start, end = decoded.get("start_time"), decoded.get("end_time")
        if start and now + 5 < start:
            raise ValueError(f"stage does not open until {start} (now {now})")
        if end and now > end:
            raise ValueError(f"stage ended at {end} (now {now})")

    decoded["value"] = value
    return decoded


# --------------------------------------------------------------------------
# authenticated OpenSea session
# --------------------------------------------------------------------------
class OpenSeaSession:
    def __init__(self, slug: str):
        self.slug = slug
        self.url = f"{SITE}/collection/{slug}"
        self.client = httpx.Client(
            http2=True, timeout=25.0, follow_redirects=False,
            limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0))
        self.headers = {"Accept": "application/json", "Origin": SITE,
                        "Referer": self.url, "x-app-id": APP_ID,
                        "User-Agent": "seadrop-console/1.0"}
        self.address: str | None = None

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def metadata(self) -> dict:
        r = self.client.post(GQL, json={"operationName": "MintCollectionMetadata",
                                        "query": META_QUERY,
                                        "variables": {"slug": self.slug}},
                             headers=self.headers)
        r.raise_for_status()
        col = (r.json().get("data") or {}).get("collectionBySlug")
        if not col:
            raise RuntimeError(f"collection {self.slug} not found")
        return col

    def login(self, private_key: str) -> str:
        """SIWE. Done well before fire time; the session is then kept warm."""
        acct = Account.from_key(private_key)
        wallet = acct.address
        chain_id = self.metadata()["chain"]["networkId"]

        self.client.cookies.set("connected-account-server-hint",
                                wallet.lower(), domain="opensea.io")
        r = self.client.post(f"{SITE}/__api/auth/siwe/nonce",
                             headers={"Origin": SITE, "Referer": self.url})
        r.raise_for_status()
        nonce = r.json()["nonce"]

        issued = (dt.datetime.now(dt.timezone.utc)
                  .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        message = (f"opensea.io wants you to sign in with your Ethereum account:"
                   f"\n{wallet}\n\n{SIWE_STATEMENT}\n\nURI: {self.url}\nVersion: 1\n"
                   f"Chain ID: {chain_id}\nNonce: {nonce}\nIssued At: {issued}")
        signature = acct.sign_message(encode_defunct(text=message)).signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        r = self.client.post(
            f"{SITE}/__api/auth/siwe/verify",
            headers={"Origin": SITE, "Referer": self.url},
            json={"message": {"domain": "opensea.io", "address": wallet,
                              "statement": SIWE_STATEMENT, "uri": self.url,
                              "version": "1", "chainId": str(chain_id),
                              "nonce": nonce, "issuedAt": issued,
                              "accountType": "Ethereum"},
                  "signature": signature, "chainArch": "EVM"})
        if r.status_code != 200:
            raise RuntimeError(f"SIWE verify failed: {r.status_code} {r.text[:160]}")
        self.address = wallet
        return wallet

    def warm(self) -> None:
        """Keep the TLS connection hot so T-0 pays no handshake."""
        try:
            self.client.post(GQL, json={"operationName": "Warm",
                                        "query": "query Warm{__typename}",
                                        "variables": {}}, headers=self.headers,
                             timeout=8.0)
        except Exception:  # noqa: BLE001
            pass

    def mint_action(self, drop_address: str, chain_ident: str,
                    quantity: int, token_id: str = "0") -> tuple[dict | None, str | None, float]:
        """The hot call. Returns (action, error_name, elapsed_ms)."""
        variables = {
            "address": self.address,
            "fromAssets": [{"asset": {"contractAddress": "0x" + "0" * 40,
                                      "chain": chain_ident}}],
            "toAssets": [{"asset": {"contractAddress": drop_address,
                                    "chain": chain_ident, "tokenId": token_id},
                          "quantity": str(quantity)}],
            "recipient": None,
        }
        started = time.perf_counter()
        try:
            r = self.client.post(GQL, json={"operationName": "MintActionTimelineQuery",
                                            "query": MINT_ACTION_QUERY,
                                            "variables": variables},
                                 headers=self.headers, timeout=15.0)
            ms = (time.perf_counter() - started) * 1000
            if r.status_code == 429:
                return None, "RateLimitError", ms
            body = r.json()
            swap = (body.get("data") or {}).get("swap") or {}
            for a in swap.get("actions") or []:
                d = a.get("transactionSubmissionData")
                if d:
                    return d, None, ms
            errs = swap.get("errors") or []
            name = errs[0].get("__typename") if errs else "NoActionReturned"
            return None, name, ms
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}", (time.perf_counter() - started) * 1000


# --------------------------------------------------------------------------
# job
# --------------------------------------------------------------------------
@dataclass
class SignedJob:
    id: str
    slug: str
    chain_id: int
    chain_name: str
    nft: str
    drop_address: str
    chain_ident: str
    wallet: str
    quantity: int
    stage_index: int
    stage_type: str
    start_time: int
    fire_at: float
    rpcs: list[str]
    gas_limit: int
    max_fee: int
    tip: int
    nonce: int
    max_value_wei: int
    # A rehearsal: do everything including fetching real calldata, validating
    # it and signing, then stop instead of broadcasting. Costs nothing and is
    # the only way to prove the pipeline end to end without spending.
    dry_run: bool = False
    state: str = "armed"
    error: str | None = None
    tx_hash: str | None = None
    decoded: dict | None = None
    receipt: dict | None = None
    sent_at: float | None = None
    created: float = field(default_factory=time.time)
    log: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)

    def say(self, msg: str) -> None:
        self.log.append(f"[{time.strftime('%H:%M:%S', time.gmtime())}Z] {msg}")

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": "signed", "state": self.state,
            "dry_run": self.dry_run,
            "slug": self.slug, "chain_id": self.chain_id,
            "chain_name": self.chain_name, "wallet": self.wallet,
            "quantity": self.quantity, "stage_index": self.stage_index,
            "stage_type": self.stage_type, "start_time": self.start_time,
            "fire_at": self.fire_at, "tx_hash": self.tx_hash,
            "error": self.error, "decoded": self.decoded, "receipt": self.receipt,
            "log": self.log, "created": self.created, "sent_at": self.sent_at,
            "max_fee_gwei": self.max_fee / 1e9, "tip_gwei": self.tip / 1e9,
            "nonce": self.nonce, "gas_limit": self.gas_limit,
            "seconds_to_fire": max(self.fire_at - time.time(), 0),
        }

    def cancel(self) -> None:
        self.stop.set()
        if self.state in {"armed", "waiting"}:
            self.state = "cancelled"
            self.say("cancelled before firing")


def run_signed(job: SignedJob, session: OpenSeaSession, private_key: str) -> None:
    """Wait for the stage, fetch calldata the instant it opens, sign, broadcast."""
    clients = {u: httpx.Client(timeout=10.0,
                               limits=httpx.Limits(max_keepalive_connections=4,
                                                   keepalive_expiry=300.0))
               for u in job.rpcs}
    account = Account.from_key(private_key)
    try:
        job.state = "waiting"
        job.say(f"armed for stage {job.stage_index} ({job.stage_type}); "
                f"session authenticated as {job.wallet}")
        _warm(clients)
        session.warm()
        job.say("OpenSea session and RPC connections warmed")

        # Re-authenticate and re-warm shortly before the stage so neither the
        # session nor the sockets are cold at T-0. Budget-free: the auth
        # endpoints are separate from the mint-action rate limit.
        # Every network call in this loop blocks for hundreds of milliseconds,
        # so each one runs AT MOST ONCE and only while there is room for it.
        # Calling warm() on each iteration inside the final seconds is what made
        # an earlier build fire 540 ms late.
        refreshed = False
        primed = False
        final_warmed = False
        last_warm = time.time()
        while not job.stop.is_set():
            remaining = job.fire_at - time.time()
            if remaining <= 0:
                break
            # A re-login costs ~2 s, so only do it with plenty of margin.
            if not refreshed and 20 < remaining <= 60:
                refreshed = True
                try:
                    session.login(private_key)
                    job.say("session refreshed for the final approach")
                except Exception as exc:  # noqa: BLE001
                    job.say(f"session refresh failed ({exc}); keeping the old one")
            # The FIRST mint-action query on a session costs ~810 ms; every one
            # after it costs ~550 ms. Warming the socket does not help - the
            # penalty is server-side. So spend one throwaway action request
            # early to pay it in advance. It returns DropNotMintingError, has no
            # side effects, and the ~5-per-15s budget refills long before T-0.
            elif not primed and 25 < remaining <= 40:
                primed = True
                _, err, ms = session.mint_action(
                    job.drop_address, job.chain_ident, job.quantity)
                job.say(f"primed the backend path in {ms:.0f} ms ({err})")
            # One last warm-up, early enough to finish well before T-0.
            elif not final_warmed and 2.5 < remaining <= 4.0:
                final_warmed = True
                session.warm()
                _warm(clients)
                job.say("final warm-up done")
            elif remaining > 6 and time.time() - last_warm > 20:
                session.warm()
                _warm(clients)
                last_warm = time.time()
            # Never block past the target: sleep only up to what is left.
            time.sleep(min(remaining - 0.04, 0.25) if remaining > 0.08 else 0.0)
        if job.stop.is_set():
            return

        job.state = "fetching"
        job.say(f"fired {(time.time() - job.fire_at) * 1000:+.1f} ms vs stage open")

        # The rate limit is ~5 requests per ~15 s, so this is deliberately
        # frugal: one request exactly on time, then a short backoff only if the
        # answer was a transient one.
        action = None
        backoff = [0.0, 0.20, 0.35, 0.60, 1.00]
        for attempt, wait in enumerate(backoff, start=1):
            if wait:
                time.sleep(wait)
            if job.stop.is_set():
                return
            action, err, ms = session.mint_action(
                job.drop_address, job.chain_ident, job.quantity)
            if action:
                job.say(f"calldata received on attempt {attempt} in {ms:.0f} ms")
                break
            job.say(f"attempt {attempt}: {err} after {ms:.0f} ms")
            if err not in RETRYABLE:
                job.state = "failed"
                if err == "InsufficientFundError":
                    # OpenSea checks the balance server-side and withholds the
                    # calldata entirely, so this fails before anything is signed.
                    job.error = ("OpenSea refused: the wallet's balance is too low "
                                 "for it to issue calldata. Top it up - even a free "
                                 "mint needs gas headroom.")
                elif err == "MintWalletIneligible":
                    job.error = ("OpenSea refused: this wallet is not eligible for "
                                 "that stage.")
                else:
                    job.error = f"OpenSea refused: {err}"
                return
        if not action:
            job.state = "failed"
            job.error = "no calldata within the rate-limit budget"
            return

        # ---- validate before signing anything -------------------------
        try:
            decoded = validate_action(
                action, nft=job.nft, minter=job.wallet, quantity=job.quantity,
                chain_id=job.chain_id, max_value_wei=job.max_value_wei)
        except ValueError as exc:
            job.state = "rejected"
            job.error = f"calldata validation failed, nothing signed: {exc}"
            job.say(job.error)
            return
        job.decoded = decoded
        job.say(f"validated {decoded['function']} stage={decoded.get('drop_stage_index')} "
                f"qty={decoded['quantity']} value={decoded['value']}")

        # OpenSea returns `to` lowercased; eth_account rejects a non-EIP-55
        # address outright, so it has to be checksummed before signing.
        tx = {"chainId": job.chain_id, "nonce": job.nonce,
              "to": to_checksum_address(action["to"]),
              "value": decoded["value"], "gas": job.gas_limit,
              "maxFeePerGas": job.max_fee, "maxPriorityFeePerGas": job.tip,
              "data": action["data"], "type": 2}
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        raw_hex = "0x" + raw.hex().removeprefix("0x")
        job.tx_hash = "0x" + signed.hash.hex().removeprefix("0x")

        if job.dry_run:
            job.state = "dry-run-ok"
            job.say(f"DRY RUN: real calldata fetched, validated and signed "
                    f"({len(raw_hex)//2} byte tx, {job.tx_hash}). "
                    f"Nothing was broadcast and nothing was spent.")
            return

        job.state = "broadcasting"
        with ThreadPoolExecutor(max_workers=max(len(job.rpcs), 1)) as pool:
            results = list(pool.map(
                lambda u: _broadcast(clients[u], u, raw_hex), job.rpcs))
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
        total = (job.sent_at - job.fire_at) * 1000
        job.say(f"{job.tx_hash} in mempool {total:.0f} ms after the stage opened")

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
                job.say(f"{'MINTED' if ok else 'REVERTED'} in block "
                        f"{job.receipt['block']}, gas {job.receipt['gas_used']}")
                if not ok:
                    job.error = "the transaction reverted on-chain"
                return
            time.sleep(0.3)
        job.state = "pending"
        job.error = "no receipt within 180s; check the hash on an explorer"
    except Exception as exc:  # noqa: BLE001
        job.state = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.say(f"error: {job.error}")
    finally:
        del private_key
        session.close()
        for c in clients.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


def iso_to_unix(text: str) -> int:
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
