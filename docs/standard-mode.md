# Standard mode: the external CLI interface

Turbo mode needs nothing but an RPC endpoint. Standard mode is different: it is
a **driver**, and it expects an external command-line mint tool to be installed
separately. That tool is not part of this repository and is not distributed
with it.

This document describes the interface the driver expects, so you can tell
whether a given CLI will work with it — or write one.

## Why a driver at all

Turbo mode only covers **public** SeaDrop stages, because those are the ones
whose calldata is fully determined on-chain. Allowlist and signed-presale
stages require a signature that only OpenSea issues, and there is no way to
pre-compute that. Standard mode exists to cover those cases, at the cost of the
~530 ms OpenSea round trip described in [latency.md](latency.md).

## Expected invocation

```
<binary> mint      # interactive scheduling session
<binary> doctor    # validate config, wallet and RPC; exit 0 on success
```

The console runs the binary with an explicit working directory (`OSNM_WORKDIR`),
because the tool is expected to locate its config by walking up from the current
directory.

## Expected configuration file

A `.env` in the working directory. The driver writes only these keys and
refuses to write anything outside this set, since a stricter tool will reject
unknown settings:

```
WALLET_KEY  RPC_URL  FEE_AUTOMATIC  GAS_LIMIT
MAX_FEE_PER_GAS_GWEI  MAX_PRIORITY_FEE_PER_GAS_GWEI  REPLACEMENT_BUMP_BPS
SCHEDULE_REFRESH_INTERVAL_SECONDS  TRANSACTION_MAX_ATTEMPTS
PENDING_TIMEOUT_SECONDS  RECEIPT_POLL_BASE_DELAY_MS  RECEIPT_POLL_MAX_DELAY_MS
OPENSEA_REQUEST_TIMEOUT_MS  ELIGIBILITY_REQUEST_TIMEOUT_MS
OPENSEA_MAX_ATTEMPTS  OPENSEA_RETRY_INTERVAL_MS  OPENSEA_CALLDATA_MAX_ATTEMPTS
WALLETS_FILE  SPONSORED  RECIPIENT_ADDRESS  SPONSOR_KEY
SPONSORED_EXECUTOR_ADDRESS  SPONSORED_OPERATION_DEADLINE_SECONDS
```

Only `WALLET_KEY`, `RPC_URL`, `FEE_AUTOMATIC` and `GAS_LIMIT` are actually used
by this console. The multi-wallet and sponsored keys are listed only so the
writer does not clobber them if they already exist; **the console never sets
them**, and enabling that class of feature is out of scope here.

## Expected prompt sequence

The driver answers prompts by matching the **tail** of the output stream. The
prompts are written without a trailing newline and flushed, so matching is on
`endswith`, not on lines.

| Order | Prompt | Driver sends |
|---|---|---|
| 1 | `Mint target: ` | the collection slug |
| 2 | `Phases: ` | comma-separated phase numbers |
| 3 | `Token ID: ` | token id (ERC-1155 style stages only) |
| 4 | `Quantity: ` | the quantity |
| 5 | `Answer [y/N]: ` | `y` |

Two behaviours the driver has to tolerate, both of which will silently break a
naive implementation:

- **Prompt 2 is skipped** when only one phase is selectable; the tool
  auto-selects it and prints `Selected stage N TYPE automatically.`
- **Prompt 3 only appears** for stages with a token range. ERC-721 drops skip
  it entirely.

So the driver does not assume a fixed order. It waits for whichever of
`Token ID:`, `Quantity:` or `Answer [y/N]:` appears next and responds
accordingly.

### The re-match trap

After answering a prompt the tool has not printed anything yet, so the output
still ends with the prompt just answered. Polling again immediately re-matches
it and sends the same input twice. The second copy is then consumed by the
*next* prompt — which, when that next prompt is the `y/N` confirmation, silently
cancels every mint.

The driver records the output length at the moment it answers and refuses to
match another prompt until the stream has grown past it.

## Expected phase listing

Parsed from the output before prompt 2:

```
   1. Stage 0 | PUBLIC_SALE | active | eligible | available
      start=2026-09-19T18:00:24.000Z | end=2026-10-22T18:00:24.000Z | max=5 | token=ERC-721
```

Output is ANSI-coloured even when stdout is not a terminal, so escape sequences
are stripped before matching.

## Expected key handling

The tool is expected to read `WALLET_KEY` from the `.env` file **once, at
startup**, and never from the process environment. That single property is what
lets the console keep the key on disk for ~0.11 s: it writes the key, launches
the tool, waits for the `Mint target: ` prompt — which proves the config has
already been read — and then shreds the key back out.

If your CLI re-reads its config later, this scheme is unsafe for it and you
should keep the key in place for the run instead.

## Exit behaviour

- `doctor` exits `0` on success.
- `mint` runs until its schedule completes, logging `Mint session finished.`
- Errors are printed as `[ERROR] <timestamp> <message>` on stdout/stderr.
- A cancelled setup logs `Operation cancelled.` at info level, which the console
  detects explicitly because it is not an `[ERROR]` line.
