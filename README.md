# OpenSea Turbo Minter

A self-hosted, loopback-only web console for minting NFTs from OpenSea SeaDrop
drops, with a pre-signed fast path that lands a transaction **~85 ms** after a
public stage opens instead of **~0.95 s**.

Chain selection, mint price, fee recipient and per-wallet allowance are all
discovered automatically — from the chain itself, not from an API.

![status](https://img.shields.io/badge/python-3.10%2B-blue)
![license](https://img.shields.io/badge/license-MIT-green)

---

## The problem this solves

The usual way to automate a SeaDrop mint is to ask OpenSea's GraphQL API for the
transaction calldata at fire time. That call costs **~530 ms**, measured on a
warm HTTP/2 connection. It is not a network problem — the Cloudflare edge is
1.4 ms from the test host — it is OpenSea's backend. Add the retry gap and a
transaction lands roughly 0.55–1.35 s after the stage opens, which loses a
genuinely contested first-come-first-served drop.

For a **public** SeaDrop stage none of that is necessary. Everything the
transaction needs is on-chain and knowable in advance, so it can be built and
signed *before* the stage opens. At T−0 the only work left is one
`eth_sendRawTransaction`.

| | Standard mode | Turbo mode |
|---|---|---|
| Time to mempool after open | 0.55 – 1.35 s | **~85 ms** |
| Dominant cost | OpenSea API, ~530 ms | node-side send, ~84 ms |
| Firing accuracy vs target | — | **+0.0 ms** |
| Stage types | all, incl. allowlist & signed presale | public SeaDrop only |
| Contacts OpenSea at fire time | yes | **no** |

Full methodology, including a bug the measurements caught and a correctness
trap in `getPublicDrop`, is in **[docs/latency.md](docs/latency.md)**.

---

## How turbo works

```solidity
SeaDrop.mintPublic(
    address nftContract,
    address feeRecipient,
    address minterIfNotPayer,
    uint256 quantity
) payable
```

| Value | Read from |
|---|---|
| price, start/end time, per-wallet cap | `getPublicDrop(address)` |
| fee recipient | `getAllowedFeeRecipients(address)` |
| already minted by this wallet | `getMintStats(address)` |
| nonce, base fee, balance | the RPC |

The console builds that calldata, signs an EIP-1559 transaction, holds it, and
broadcasts to every configured endpoint in parallel the moment the on-chain
`startTime` passes. Connections are pooled and warmed on arm, again every 20 s,
and once more at T−1.5s, so no TLS handshake happens at the fire moment.

Before writing any of it the calldata was validated by simulating against a live
drop: 132 bytes, `eth_call` → success, `estimateGas` 112,573.

---

## Supported chains

SeaDrop sits at the same deterministic address on all of these, verified by
reading `eth_getCode` (21,081 bytes on every one), so turbo works everywhere:

| Chain | ID | Gas token |
|---|---|---|
| Ethereum | 1 | ETH |
| Optimism | 10 | ETH |
| Polygon | 137 | POL |
| Robinhood | 4663 | ETH |
| Arc | 5042 | USDC |
| Base | 8453 | ETH |
| Arbitrum One | 42161 | ETH |
| Ink | 57073 | ETH |

Arc settles gas in USDC but its native balance still uses 18 decimals (checked
against real on-chain transaction values), so amounts display normally.

Paste an Alchemy key once and the console fills every network's endpoint, then
reports which ones actually answer. Each field also accepts a comma-separated
list; turbo fires at all of them and takes whichever accepts first.

Turbo can also take a raw `0x` contract plus an explicit chain, which skips
OpenSea entirely — useful on chains OpenSea does not index.

---

## Private key handling

This is the part worth reading carefully.

**No key is ever stored.** Every mint asks for it again, and you can use a
different wallet each time.

**Standard mode.** The external CLI is expected to read its key from a `.env`
file — never from the process environment — and to read it exactly once at
startup. So the console writes `WALLET_KEY`, launches it, and waits for the
`Mint target:` prompt, which proves the config has already been read. Only then
does it shred the key back out. Keying the scrub off the CLI's own prompt rather
than a fixed timeout is what keeps the window short: measured at **~0.11 s**,
verified with a 50 ms poller.

**Turbo mode.** Pre-signing is impossible without signing, so the key is used in
the console's own process. It is never written to disk and never returned by any
endpoint, but this is a weaker position than Standard, where the key only ever
reached a separate short-lived process. If that trade matters more than the
speed, use Standard.

A header chip shows `key on disk: no` at all times; `YES` means a run died
mid-launch and the service should be restarted.

---

## Safety rails

Turbo refuses to arm when:

- the contract has no SeaDrop public drop configured — `getPublicDrop` returns
  six zero words rather than reverting for an unregistered contract, which
  naively parsed looks like a free mint opening at unix 0
- the public stage has already ended
- the wallet cannot cover the worst-case cost (value + gas at the fee ceiling)
- the per-wallet allowance is already used up
- a live `eth_call` simulation actually reverts

Nothing is signed or broadcast without an explicit confirmation step.

---

## Install

Ubuntu host, as root:

```bash
git clone https://github.com/0elyas0/Opensea-turbo-minter.git
cd Opensea-turbo-minter
sudo bash deploy/install.sh
```

Then from your own machine:

```bash
ssh -N -L 8787:127.0.0.1:8787 <user>@<your-server>
```

and open <http://127.0.0.1:8787>.

The console binds to `127.0.0.1` only and refuses non-loopback `Host` headers,
so it is not reachable from the internet and cannot be hit by DNS rebinding.
**Do not expose it publicly.**

### Standard mode needs an external mint CLI

**Turbo mode is self-contained** — it talks only to the chain and needs nothing
else installed.

Standard mode is a driver, not a minter. It shells out to a separate
command-line mint tool, feeds it the collection, phase and quantity by
answering its interactive prompts, and streams its output back to the browser.
That external tool is not part of this repository and is not distributed with
it. Supply your own and point the console at it:

```bash
# any CLI exposing `mint`, `doctor` and a .env-based config works
export OSNM_BIN=/usr/local/bin/opensea-mint
```

The expected interface is documented in [docs/standard-mode.md](docs/standard-mode.md).

Whatever you use, review it first — it reads a private key.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OSNM_BIN` | `/usr/local/bin/opensea-mint` | external mint CLI (Standard mode only) |
| `OSNM_WORKDIR` | `~/mint-cli` | directory holding `.env` |
| `OSNM_STATE_DIR` | `~/.osnm-ui` | job logs and network config |

`OSNM_WORKDIR` matters: the CLI is expected to resolve `.env` by walking up from
the current directory, so jobs are launched with an explicit `cwd` rather than
inheriting one.

---

## Known limitations

- **Turbo covers public stages only.** Allowlist and signed-presale stages need
  a signature that only OpenSea issues, so those go through Standard mode.
- **Turbo jobs are in-process threads.** They survive the browser closing but
  *not* a restart of the service. Arm a few minutes before the drop, not hours.
- **Nonce is captured at arm time.** Sending anything else from that wallet
  after arming invalidates the signed transaction.
- **Price is read at arm time.** A creator who changes the drop config before
  open will make the prepared value wrong.
- **The fee ceiling is fixed at arm time** (base fee × multiplier + tip).
- Standard mode's funding check is advisory; the CLI runs the authoritative one.

---

## Disclaimer

This software signs and broadcasts real blockchain transactions that spend real
funds. It comes with no warranty of any kind. Minting is competitive and
frequently unprofitable. Use a wallet funded with only what you intend to spend,
review the code before running it, and understand that a reverted transaction
still costs gas.

Licensed under the [MIT License](LICENSE).
