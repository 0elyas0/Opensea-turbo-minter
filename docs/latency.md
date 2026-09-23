# Latency: why the turbo path exists

All figures were measured from a 1-vCPU VPS in Tokyo against a private Alchemy
endpoint, not estimated. They are reproducible with the scripts described at the
bottom.

## The question

Can a mint bot that fetches its calldata from OpenSea's API win a first-come,
first-served drop that sells out in under a second?

## Measurement 1 — where the time actually goes

`opensea-mint` is well engineered around the hot path: at T−10s it authenticates
and captures nonce, fees, balance and eligibility, explicitly logging that this
happens *outside the hot path*. At T−2s it begins asking OpenSea for the mint
calldata, retrying every 250 ms while the API returns `MintStageNotOpen`.

So the fire-time cost is one OpenSea GraphQL round trip. Measured on a warm,
reused HTTP/2 connection:

| Hop | min | median | p90 | max |
|---|---|---|---|---|
| `gql.opensea.io` GraphQL | 475 ms | **529 ms** | 557 ms | 577 ms |
| Alchemy `eth_blockNumber` | 8 ms | **11 ms** | 13 ms | 40 ms |

The obvious hypothesis — "the VPS is far from OpenSea" — is wrong:

```
ping gql.opensea.io  →  1.126 / 1.399 / 2.055 ms
```

The Cloudflare edge is 1.4 ms away. The ~530 ms is OpenSea's backend. No VPS
relocation, no private RPC and no tuning changes it.

### Resulting timeline

```
stage opens
  ├─ wait for the next poll cycle    0 – 780 ms   (530 ms request + 250 ms gap)
  ├─ the successful calldata request ~530 ms
  └─ sign + broadcast                ~15 ms
                                     ─────────────
                       best ~0.55 s   typical ~0.95 s   worst ~1.35 s
```

## Measurement 2 — the alternative

A public SeaDrop stage needs none of that. The call is:

```solidity
SeaDrop.mintPublic(
    address nftContract,
    address feeRecipient,
    address minterIfNotPayer,
    uint256 quantity
) payable
```

Every argument is readable from the chain in advance:

| Value | Source |
|---|---|
| price, start/end time, per-wallet cap | `getPublicDrop(address)` |
| fee recipient | `getAllowedFeeRecipients(address)` |
| already minted | `getMintStats(address)` |

So the transaction can be built **and signed before the stage opens**. At T−0
the only remaining work is one `eth_sendRawTransaction`.

Verified before writing any of it, by simulating against a live drop:

```
calldata      132 bytes            (byte-identical to a known-good implementation)
eth_call      → 0x                 (would succeed)
estimateGas   112,573
```

## Measurement 3 — a bug the measurements caught

The first turbo firing test showed **+0.0 ms** accuracy against the target
timestamp, but a 135 ms broadcast. `httpx.post()` builds a fresh client per
call, so the send was paying a TLS handshake at the worst possible moment.

After adding pooled clients that are warmed on arm, re-warmed every 20 s, and
warmed once more at T−1.5s, the same test gave 85 ms. Still not the 11 ms of a
read call, which raised the obvious question: is the remainder our connection,
or the node?

Both methods on the **same warm socket**:

| Method | min | median | max |
|---|---|---|---|
| `eth_blockNumber` | 7.5 ms | **10.2 ms** | 36.3 ms |
| `eth_sendRawTransaction` | 81.6 ms | **84.0 ms** | 125.5 ms |

So the remaining ~84 ms is node-side validation and propagation, not our
connection. That is the floor from the client side, and it is honest to say so
rather than claim 11 ms.

## Result

| | Standard (via the CLI) | Turbo (pre-signed) |
|---|---|---|
| Time to mempool after open | 0.55 – 1.35 s | **~85 ms** |
| Dominant cost | OpenSea API, ~530 ms | node send, ~84 ms |
| Firing accuracy | n/a | **+0.0 ms** |
| Works for | every stage type | public SeaDrop stages only |

Roughly a 6–15× improvement, with the remaining cost sitting outside the
client's control.

## A correctness trap worth recording

`getPublicDrop` on a contract that was never registered with SeaDrop does **not
revert** — it returns six zero words. Parsed naively that looks like a free mint
that opened at unix 0, so an armed job would fire immediately into a guaranteed
revert and burn gas. The plan builder rejects any drop whose start, end and cap
are all zero.

## Reproducing

Each number above came from a short standalone script run on the host:
timing comparisons use `httpx.Client` with `http2=True` and a warm-up request
before sampling; firing accuracy is measured by arming a job whose signed
transaction is funded by an empty wallet, so the node rejects it after the full
sign → wait → broadcast path has executed, with no funds at risk.
