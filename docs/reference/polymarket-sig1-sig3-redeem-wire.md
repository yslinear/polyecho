# Polymarket sig 1 (POLY_PROXY / Magic) + sig 3 (POLY_1271 / Deposit Wallet) redeem wire format

**Status**: VERIFIED from upstream `Polymarket/builder-relayer-client` (TS reference impl). Python SDK `py-builder-relayer-client==0.0.1` only ships SAFE; PROXY + DEPOSIT-WALLET must be reimplemented from these TS sources.

**Sources**
- TS client: <https://github.com/Polymarket/builder-relayer-client/blob/main/src/client.ts>
- Endpoints + types: <https://github.com/Polymarket/builder-relayer-client/blob/main/src/endpoints.ts>, <https://github.com/Polymarket/builder-relayer-client/blob/main/src/types.ts>
- Derive logic (CREATE2): <https://github.com/Polymarket/builder-relayer-client/blob/main/src/builder/derive.ts>
- Proxy signing: <https://github.com/Polymarket/builder-relayer-client/blob/main/src/builder/proxy.ts>
- Deposit-wallet signing: <https://github.com/Polymarket/builder-relayer-client/blob/main/src/builder/deposit-wallet.ts>
- Config (factory addresses): <https://github.com/Polymarket/builder-relayer-client/blob/main/src/config/index.ts>

---

## Common wire facts (both sigs)

- **Base URL**: `https://relayer-v2.polymarket.com` (production) — already in `shared/constants.py`.
- **Endpoints** (all relative):
  - `GET /nonce?address=<EOA>&type=<SAFE|PROXY|WALLET>` → `{ nonce: string }`
  - `GET /relay-payload?address=<EOA>&type=PROXY` → `{ address, nonce }` (PROXY only — supplies the relay address used in struct hash)
  - `POST /submit` (auth) — body = one of `TransactionRequest` / `DepositWalletBatchRequest` / `DepositWalletCreateRequest`
  - `GET /transaction?id=<txID>` → poll until state ∈ `STATE_MINED|STATE_CONFIRMED` (failure: `STATE_FAILED|STATE_INVALID`)
  - `GET /deployed?address=<wallet>[&type=WALLET]` → `{ deployed: bool }`
- **Auth**: optional builder HMAC headers (`POLY_BUILDER_API_KEY` / `POLY_BUILDER_SIGNATURE` / `POLY_BUILDER_TIMESTAMP` / `POLY_BUILDER_PASSPHRASE`) computed by `builder-signing-sdk` / `py-builder-signing-sdk`. The Python builder-signing-sdk **is supported** and orthogonal to tx-type, so use it as-is.
- **Polygon (chainId=137) factory addresses** (verified in `src/config/index.ts`):
  - SafeFactory `0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b`
  - ProxyFactory (Magic) `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052`
  - RelayHub `0xD216153c06E857cD7f72665E0aF1d7D82172F494`
  - DepositWalletFactory `0x00000000000Fb5C9ADea0298D729A0CB3823Cc07`
  - DepositWalletImplementation `0x58CA52ebe0DadfdF531Cde7062e76746de4Db1eB`
- **Init-code hashes** (`src/constants/index.ts`): `SAFE_INIT_CODE_HASH=0x2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf`, `PROXY_INIT_CODE_HASH=0xd21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b`.

---

## sig 1 — POLY_PROXY (Magic)

**Funder derivation** — VERIFIED, pure CREATE2 (no RPC call needed):
```
salt = keccak256(abi.encodePacked(address EOA))    # NOTE: encodePacked, not encodeAbiParameters
proxy = create2(from=ProxyFactory, salt, bytecodeHash=PROXY_INIT_CODE_HASH)
```
Python: `eth_utils.keccak(bytes.fromhex(eoa[2:]))` for salt; assemble `0xff || factory || salt || initCodeHash` then keccak, take last 20 bytes.

**Redeem flow** (one POST):
1. `from = EOA` (signer EOA recovered from your private key, NOT the proxy).
2. `nonce` payload from `GET /relay-payload?type=PROXY` → returns `{ address: relay, nonce }`.
3. Encode batch via `ProxyFactory.proxy([(to, typeCode=1 Call, data, value=0)])` — ABI is `proxy((address,uint8,bytes,uint256)[])` (see `src/abis/proxyFactory.ts`). For CTF redeem put `to=0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` and `data=redeemPositions(usdc, 0x00, conditionId, [1,2])`.
4. Compute struct hash (eth_personal_sign over raw bytes):
   ```
   keccak256( "rlx:" || from(20) || to=ProxyFactory(20) || data || txFee=0(32) || gasPrice(32)
              || gasLimit(32) || nonce(32) || relayHub(20) || relay(20) )
   ```
5. Sign with `eth_sign` (i.e. `web3.eth.account.signHash(...)`) — **not EIP-712**. That hashed message is what the relayer recovers against `from`.
6. `POST /submit` JSON:
   ```json
   {
     "type": "PROXY",
     "from": "<EOA>",
     "to":   "<ProxyFactory>",
     "proxyWallet": "<derived proxy>",
     "data": "<encoded proxy(...) calldata>",
     "nonce": "<nonce>",
     "signature": "0x...65bytes",
     "signatureParams": {
       "gasPrice": "0", "gasLimit": "<eth_estimateGas or 10_000_000>",
       "relayerFee": "0", "relayHub": "<RelayHub>", "relay": "<relay from /relay-payload>"
     },
     "metadata": "redeem positions"
   }
   ```
7. Poll `GET /transaction?id={transactionID}` until `STATE_MINED`.

**Auto-deploy**: Proxy is deployed on first tx by the factory — no separate create call needed (confirmed in TS README: "Proxy wallets are deployed automatically on its first transaction").

## sig 3 — POLY_1271 (Deposit Wallet)

**Funder derivation** — VERIFIED, CREATE2 over ERC-1967 init code (Solady `LibClone.initCodeHashERC1967`):
```
walletId = bytes32(left-pad owner address to 32)
args     = abi.encode(address factory, bytes32 walletId)        # NOT packed
salt     = keccak256(args)
bytecodeHash = keccak256(
    bytes10(0x61003d3d8160233d3973 | (n << 56))                   # n = len(args)/32 = 64? (n = byte-length of args)
  || implementation(20)
  || 0x6009
  || 0x5155f3363d3d373d3d363d7f360894a13ba1a3210667c828492db98dca3e2076   # ERC1967_CONST2
  || 0xcc3735a920a3ca505d382bbc545af43d6000803e6038573d6000fd5b3d6000f3   # ERC1967_CONST1
  || args
)
deposit_wallet = create2(from=factory, salt, bytecodeHash)
```
**Critical detail**: `n = (args.length - 2) / 2` in TS = byte-count of `args` (=64 for two 32-byte words). The `0x6009` and the two `ERC1967_CONST*` blobs are **literal byte constants from Solady v0.1.26** — must be copied verbatim. Add a fixture test that compares Python output to a known production deposit-wallet address to catch any bit-flip.

**Deploy** (required, NOT auto on first tx):
```json
POST /submit
{ "type": "WALLET-CREATE", "from": "<EOA>", "to": "<DepositWalletFactory>" }
```
No signature — relayer derives + deploys for the caller's EOA. Idempotent if already deployed (or check via `GET /deployed?address=<wallet>&type=WALLET` first).

**Redeem flow** — EIP-712 batch:
1. `nonce` from `GET /nonce?address=<EOA>&type=WALLET`.
2. `deadline = now + 240` (seconds; TS uses 4-min window).
3. Build `calls: [{target, value:"0", data}]` — each call is `{target,value,data}` (note `target` not `to`). For CTF redeem the call is the same `redeemPositions(...)` calldata as sig 1 but `target = CTFExchange`.
4. EIP-712 sign:
   ```
   domain = { name:"DepositWallet", version:"1", chainId:137, verifyingContract:<walletAddress> }
   types  = {
     Call: [{target address},{value uint256},{data bytes}],
     Batch:[{wallet address},{nonce uint256},{deadline uint256},{calls Call[]}]
   }
   message = { wallet:<walletAddress>, nonce, deadline, calls }
   primaryType = "Batch"
   ```
   Use `eth_account.messages.encode_typed_data` / `Account.sign_typed_data`.
5. `POST /submit`:
   ```json
   {
     "type": "WALLET",
     "from": "<EOA>",
     "to":   "<DepositWalletFactory>",
     "nonce": "<nonce>",
     "signature": "0x...",
     "depositWalletParams": {
       "depositWallet": "<walletAddress>",
       "deadline": "<unix>",
       "calls": [{"target":"<CTF>","value":"0","data":"<redeem calldata>"}]
     }
   }
   ```

---

## PolyEcho implementation recommendation

**Do NOT fork py-builder-relayer-client**. v0.0.1 is missing `builder/proxy.py`, `builder/deposit_wallet.py`, all WALLET/PROXY constants, plus the ERC-1967 init-code-hash routine — at that point you'd rewrite ~70% of it.

**Recommended path**: write a thin standalone module `packages/shared/src/shared/polymarket/relayer_extended.py` (≤400 LOC) that:
- Reuses existing `py-builder-relayer-client` HTTP client + signing-sdk auth for the SAFE path (don't break sig 2).
- Adds two new redeem functions `redeem_proxy(...)` and `redeem_deposit_wallet(...)` posting hand-rolled JSON to `/submit`.
- Imports init-code hashes + factory addresses from a `polymarket_constants.py` module that mirrors `src/config/index.ts` and `src/constants/index.ts` **verbatim**, with a single source-of-truth comment pointing back to the TS commit SHA.
- Ships a unit test that, given a known EOA→proxy and EOA→deposit-wallet mapping from a mainnet redeem we can grep on PolygonScan, asserts CREATE2 derivation byte-for-byte.

**Risks + fail-closed guards**
- **Wire drift risk: LOW for sig 1, MEDIUM for sig 3**. Sig 1 uses the legacy RelayHub struct hash unchanged since 2021. Sig 3 is the new (~2025) deposit-wallet flow and Polymarket can still iterate; the EIP-712 schema name "Batch"/"Call" or domain version "1" could bump.
- Required guards before live execution:
  1. Pin the Polymarket TS commit SHA you copied from in code + CI check `gh api repos/Polymarket/builder-relayer-client/commits/main` weekly; alert on drift.
  2. Before posting `/submit`, **re-derive** the funder and assert it matches the `signature_type=1`/`signature_type=3` funder stored in our DB; refuse if mismatch.
  3. Refuse to post if `gas_limit > 30_000_000`, `value != "0"`, `deadline - now > 600s`, or target ∉ allow-list `{CTF, NegRiskAdapter}` — these are the four ways a buggy encoder could silently send funds elsewhere.
  4. After `POST /submit`, fail-closed if `pollUntilState` doesn't reach `STATE_MINED` within 5 min — never assume success from `STATE_NEW`.

**UNVERIFIED items requiring manual confirmation before mainnet**
- Whether `to` in PROXY submit JSON must be the `ProxyFactory` or the user's proxy itself. TS code says factory; double-check by intercepting a real Magic-user redeem via `chrome-devtools` MCP against `polymarket.com` (sniff `POST relayer-v2.polymarket.com/submit`).
- Whether RPC `eth_estimateGas` is acceptable from a server-side relayer caller, or whether Polymarket expects the constant `10_000_000` fallback. Try estimate first; fall back on revert.
- Deposit-wallet ERC-1967 `n` byte: TS uses `BigInt((args.length - 2) / 2)` against a hex string — Python equivalent must use `len(args_bytes)` (=64), then shift `<< 56`. Verify with a fixture test against `0x...` mainnet deposit wallet.
