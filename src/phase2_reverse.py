"""
Phase 2 reverse transition: LONG_ON_HL → PARKED_IN_LP.

Pipeline (mirror of phase2_forward):
  1. HL.market_close ETH-PERP        position → margin USDC on HC
  2. spot_send USDC HC → HyperEVM    via system address 0x20...000 (USDC idx=0)
  3. Wait USDC on HyperEVM EOA       ~few seconds
  4. CCTP V2 burn USDC HyperEVM      destDomain=6 (Base)
  5. Iris attestation                ~5-30s
  6. Wait USDC mint on Base          auto-relayer
  7. V3 swap half USDC → ETH         on Base
  8. POSM.mint new LP NFT            ETH/USDC pool full-range

Run:
  DRY_RUN=true  python -m src.phase2_reverse
  DRY_RUN=false python -m src.phase2_reverse
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

import requests
from eth_account import Account
from web3 import Web3

from . import config
from .hl_executor import HLExecutor
from .on_chain.cctp import (
    CCTPBridge, TOKEN_MESSENGER_V2, DOMAIN_BASE, DOMAIN_HYPEREVM, IRIS_API,
    TOKEN_MESSENGER_V2_ABI, address_to_bytes32,
    FINALITY_FAST,
)
from .on_chain.client import (
    BaseClient, USDC_BASE, NATIVE_ETH, HOOK_ETH_USDC, POSM,
    make_pool_key_eth_usdc, compute_pool_id, read_pool_slot0,
)
from .on_chain.lp_manager import LPManager
from .on_chain.swap_v3 import V3Swap
from .on_chain.liquidity_math import (
    liquidity_from_amounts, tick_to_sqrt_price_x96,
    amount0_from_liquidity, amount1_from_liquidity,
)
from .pnl_tracker import PnLTracker

log = logging.getLogger("phase2_reverse")

# HyperEVM constants
HYPEREVM_RPC = "https://rpc.hyperliquid.xyz/evm"
HYPEREVM_CHAIN_ID = 999
USDC_HYPEREVM = Web3.to_checksum_address("0xb88339CB7199b77E23DB6E890353E22632Ba630f")

# HL system address for USDC (token index 0): 0x2 prefix + 19 zero bytes
USDC_SYSTEM_ADDR = "0x2000000000000000000000000000000000000000"

# Reserve LP target — how much $ we want in LP after reverse cycle.
# Set to use most of what comes back, leave small buffer.
LP_RESERVE_USD = 1.0     # keep $1 on agent Base wallet as buffer
GAS_RESERVE_ETH_WEI = 1_000_000_000_000_000  # 0.001 ETH

# Full-range ticks for ETH/USDC pool (ts=60)
TICK_LOWER = -887220
TICK_UPPER =  887220
SAFETY_BUFFER_BPS = 100  # 1% slippage tolerance on mint

ERC20_ABI = [
    {"inputs": [{"name": "a", "type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"type": "bool"}],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def hyperevm_w3():
    return Web3(Web3.HTTPProvider(HYPEREVM_RPC))


def hyperevm_usdc_balance(addr: str) -> int:
    w3 = hyperevm_w3()
    c = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    return c.functions.balanceOf(Web3.to_checksum_address(addr)).call()


def hl_spot_usdc(addr: str) -> float:
    r = requests.post(f"{config.HL.api_url}/info",
                       json={"type": "spotClearinghouseState", "user": addr},
                       timeout=10)
    balances = r.json().get("balances", []) if r.status_code == 200 else []
    return next((float(b["total"]) for b in balances
                  if b.get("coin") == "USDC"), 0.0)


def run_reverse():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log.info("=" * 60)
    log.info("Phase 2 REVERSE: LONG_ON_HL → PARKED_IN_LP")
    log.info(f"  DRY_RUN: {config.RISK.dry_run}")
    log.info("=" * 60)

    base = BaseClient()
    lp   = LPManager(base)
    swap = V3Swap(base)
    cctp = CCTPBridge(base)
    hl   = HLExecutor()
    tracker = PnLTracker()

    # ─── Pre-flight ────────────────────────────────────────────
    pos = hl.get_position()
    pre_base_eth  = base.w3.eth.get_balance(base.address)
    pre_base_usdc = base.balance(USDC_BASE)
    pre_evm_usdc  = hyperevm_usdc_balance(base.address)
    pre_hl_spot   = hl_spot_usdc(base.address)

    log.info(f"\n--- Pre-flight ---")
    log.info(f"  HL position:    {pos.size_eth if pos else 0:+.4f} ETH @ ${pos.entry_px if pos else 0:.2f}")
    log.info(f"  HL spot USDC:   ${pre_hl_spot:.4f}")
    log.info(f"  HyperEVM USDC:  ${pre_evm_usdc/1e6:.4f}")
    log.info(f"  Base USDC:      ${pre_base_usdc/1e6:.4f}")
    log.info(f"  Base ETH:       {pre_base_eth/1e18:.6f}")

    # ─── Step 1: Close ETH-PERP position ───────────────────────
    log.info(f"\n--- Step 1: close ETH-PERP ---")
    if pos is None or abs(pos.size_eth) < 1e-9:
        log.info("  no position to close, skipping")
    else:
        close_res = hl.close_position()
        log.info(f"  result: {close_res.get('status')}")
        if close_res.get("status") not in ("submitted", "dry_run", "noop"):
            log.error(f"  close failed: {close_res}")
            return
        if config.RISK.dry_run:
            log.info("  [DRY_RUN] would close")
        else:
            # Record close in SQLite
            trade_id = tracker.open_trade_id()
            exit_price = hl.get_mid_price()
            if trade_id and exit_price == exit_price:
                tracker.close_trade(
                    trade_id=trade_id,
                    ts=datetime.utcnow().isoformat(),
                    price=exit_price,
                    reason="phase2_reverse",
                    raw=close_res,
                )
            time.sleep(2)
            # Verify position closed
            pos_after = hl.get_position()
            if pos_after and abs(pos_after.size_eth) > 1e-9:
                log.warning(f"  position still open: {pos_after.size_eth}")

    # ─── Step 2: spot_send USDC HC → HyperEVM ────────────────
    log.info(f"\n--- Step 2: spot_send USDC HC → HyperEVM ---")
    post_close_spot = hl_spot_usdc(base.address)
    log.info(f"  HL spot USDC after close: ${post_close_spot:.4f}")
    # Round to 2 decimals (cents). HL `send_asset` rejects amounts whose
    # float representation has a long fractional tail with
    # "Invalid number of decimals". USDC supports 8 decimals so 2 is safe.
    amount_to_send = round(post_close_spot - 0.10, 2)

    if amount_to_send < 1.0:
        log.error(f"  insufficient HL spot USDC (${amount_to_send:.4f} < $1) — aborting")
        return

    if config.RISK.dry_run:
        log.info(f"  [DRY_RUN] would spot_send ${amount_to_send:.4f} → {USDC_SYSTEM_ADDR}")
    else:
        # spot_transfer to system address triggers HyperEVM bridge.
        # NOTE: must use MAIN wallet (not API wallet) — API wallets need
        # separate activation deposit before they can sign spotSend.
        from hyperliquid.exchange import Exchange
        wallet = Account.from_key(config.HL.private_key)
        ex = Exchange(wallet, config.HL.api_url, account_address=base.address)
        log.info(f"  send_asset ${amount_to_send:.4f} HC spot → HyperEVM (via system addr)")
        # In unified account mode, HyperEVM bridge is via sendAsset with:
        #   destination = USDC system address (0x2000...000)
        #   source_dex = destination_dex = "spot"
        # The "evm" target is implied by the system address, NOT by dex string
        # (only valid dex strings: "", "spot", or specific perp dex name).
        try:
            result = ex.send_asset(
                destination=USDC_SYSTEM_ADDR,
                source_dex="spot",
                destination_dex="spot",
                token="USDC:0x6d1e7cde53ba9467b783cb7c530ce054",
                amount=amount_to_send,
            )
            log.info(f"  result: {result}")
            if isinstance(result, dict) and result.get("status") not in ("ok", "submitted"):
                log.error(f"  send_asset rejected: {result}")
                return
        except Exception as e:
            log.error(f"  send_asset failed: {e}")
            return

    # ─── Step 3: Wait USDC on HyperEVM ─────────────────────────
    log.info(f"\n--- Step 3: poll HyperEVM USDC arrival ---")
    if config.RISK.dry_run:
        log.info("  [DRY_RUN] would poll for ~60s")
    else:
        start = time.time()
        target = int((amount_to_send - 1) * 1e6)  # rough check
        while time.time() - start < 60:
            cur = hyperevm_usdc_balance(base.address)
            if cur >= target:
                log.info(f"  HyperEVM USDC: ${cur/1e6:.4f}")
                break
            time.sleep(3)
        else:
            log.error("  timeout waiting for HyperEVM USDC")
            return

    # ─── Step 4: CCTP burn USDC HyperEVM → Base ────────────────
    log.info(f"\n--- Step 4: CCTP burn USDC on HyperEVM → Base ---")
    if config.RISK.dry_run:
        log.info("  [DRY_RUN] would burn USDC on HyperEVM with destDomain=6")
        log.info("  [DRY_RUN] skipping remaining steps (Iris + swap + mint)")
        return

    # Build CCTP burn on HyperEVM
    w3evm = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    tm_evm = w3evm.eth.contract(address=TOKEN_MESSENGER_V2, abi=TOKEN_MESSENGER_V2_ABI)
    usdc_evm = w3evm.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)

    evm_amount = usdc_evm.functions.balanceOf(base.address).call()
    log.info(f"  HyperEVM USDC to bridge: ${evm_amount/1e6:.4f}")

    nonce = w3evm.eth.get_transaction_count(base.address)
    def send_evm(fn, value=0, gas_limit=300_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from":    base.address,
            "nonce":   nonce,
            "chainId": HYPEREVM_CHAIN_ID,
            "gas":     gas_limit,
            "value":   value,
            "maxFeePerGas":         w3evm.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3evm.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"    submitted: {h.hex()}")
        r = w3evm.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"    confirmed: status={r.status}, gas={r.gasUsed}")
        return r

    # approve USDC → TokenMessenger on HyperEVM
    cur_allow = usdc_evm.functions.allowance(base.address, TOKEN_MESSENGER_V2).call()
    if cur_allow < evm_amount:
        log.info("  approving USDC → TokenMessengerV2 on HyperEVM")
        send_evm(usdc_evm.functions.approve(TOKEN_MESSENGER_V2, 2**256 - 1), gas_limit=100_000)
        time.sleep(2)

    # depositForBurn
    max_fee = evm_amount * 50 // 10_000  # 0.5%
    mint_recipient = address_to_bytes32(base.address)
    dest_caller = bytes(32)
    log.info(f"  depositForBurn: amount={evm_amount}, dest=Base(6), recipient={base.address}")
    burn_receipt = send_evm(
        tm_evm.functions.depositForBurn(
            evm_amount, DOMAIN_BASE, mint_recipient, USDC_HYPEREVM,
            dest_caller, max_fee, FINALITY_FAST,
        ),
        gas_limit=200_000,
    )
    burn_tx = burn_receipt.transactionHash.hex()
    if not burn_tx.startswith("0x"):
        burn_tx = "0x" + burn_tx

    # ─── Step 5: Wait Iris attestation ─────────────────────────
    log.info(f"\n--- Step 5: Iris attestation (HyperEVM source) ---")
    attestation = cctp.wait_for_attestation(
        burn_tx, src_domain=DOMAIN_HYPEREVM,
        max_wait_sec=300, poll_interval_sec=5,
    )
    if not attestation:
        log.error("  attestation timeout")
        return

    # ─── Step 6: Wait USDC mint on Base ────────────────────────
    log.info(f"\n--- Step 6: poll Base USDC arrival ---")
    start = time.time()
    while time.time() - start < 90:
        cur = base.balance(USDC_BASE)
        if cur >= pre_base_usdc + evm_amount * 0.95:
            log.info(f"  Base USDC arrived: ${cur/1e6:.4f}")
            break
        time.sleep(5)
    else:
        log.warning("  Base USDC arrival not detected within 90s")

    # ─── Step 7: Swap half USDC → ETH ──────────────────────────
    total_usdc = base.balance(USDC_BASE)
    log.info(f"\n--- Step 7: swap half USDC → ETH ---")
    log.info(f"  total Base USDC: ${total_usdc/1e6:.4f}")
    # Reserve LP_RESERVE_USD as buffer; split the rest 50/50 by USD value.
    # We want to end up with X USDC + (X / price) ETH for LP.
    usdc_for_lp = (total_usdc - int(LP_RESERVE_USD * 1e6)) // 2
    usdc_for_swap = total_usdc - int(LP_RESERVE_USD * 1e6) - usdc_for_lp
    log.info(f"  reserve buffer:    ${LP_RESERVE_USD:.2f}")
    log.info(f"  USDC stays for LP: ${usdc_for_lp/1e6:.4f}")
    log.info(f"  USDC → swap → ETH: ${usdc_for_swap/1e6:.4f}")

    pre_swap_eth = base.w3.eth.get_balance(base.address)
    swap_res = swap.usdc_to_eth(usdc_for_swap)
    log.info(f"  swap result: {swap_res.status}")
    if swap_res.error:
        log.error(f"  swap failed: {swap_res.error}")
        return

    # Poll until ETH balance reflects the swap output. RPC propagation
    # can lag 1-5 seconds after tx confirmation. Without this poll,
    # the next step reads stale balance and mints a smaller LP than intended.
    log.info(f"  polling ETH balance update (was {pre_swap_eth/1e18:.6f})…")
    swap_settled = False
    poll_start = time.time()
    while time.time() - poll_start < 30:
        cur = base.w3.eth.get_balance(base.address)
        # Expect at least +50% of intended swap output (sanity floor)
        if cur >= pre_swap_eth + 100_000_000_000_000:  # +0.0001 ETH min
            log.info(f"  ETH balance updated: {cur/1e18:.6f} (+{(cur-pre_swap_eth)/1e18:.6f})")
            swap_settled = True
            break
        time.sleep(1)
    if not swap_settled:
        log.warning(f"  ETH balance did not update within 30s — proceeding with current value")

    # ─── Step 8: Mint new LP NFT ───────────────────────────────
    log.info(f"\n--- Step 8: mint new POSM NFT in ETH/USDC pool ---")
    eth_for_lp = base.w3.eth.get_balance(base.address) - GAS_RESERVE_ETH_WEI
    log.info(f"  ETH available for LP: {eth_for_lp/1e18:.6f}")
    log.info(f"  USDC for LP:         ${usdc_for_lp/1e6:.4f}")

    # Pool state
    pk = make_pool_key_eth_usdc()
    pool_id = compute_pool_id(pk)
    slot0 = read_pool_slot0(base, pool_id)
    sqrt_p = slot0["sqrt_price_x96"]
    sqrt_pl = tick_to_sqrt_price_x96(TICK_LOWER)
    sqrt_pu = tick_to_sqrt_price_x96(TICK_UPPER)

    L = liquidity_from_amounts(sqrt_p, sqrt_pl, sqrt_pu, eth_for_lp, usdc_for_lp)
    a0 = amount0_from_liquidity(sqrt_p, sqrt_pu, L)
    a1 = amount1_from_liquidity(sqrt_pl, sqrt_p, L)
    a0_max = a0 + a0 * SAFETY_BUFFER_BPS // 10_000
    a1_max = a1 + a1 * SAFETY_BUFFER_BPS // 10_000
    log.info(f"  L: {L:,}")
    log.info(f"  ETH:  {a0/1e18:.6f} (max {a0_max/1e18:.6f})")
    log.info(f"  USDC: ${a1/1e6:.4f} (max ${a1_max/1e6:.4f})")

    # Ensure USDC approvals (already set from earlier mint)
    lp.setup_approvals([USDC_BASE])

    mint_res = lp.mint(
        pool_key=pk,
        tick_lower=TICK_LOWER,
        tick_upper=TICK_UPPER,
        liquidity=L,
        amount0_max=a0_max,
        amount1_max=a1_max,
        eth_value_wei=a0_max,
    )
    log.info(f"  mint result: {mint_res.status}")
    if mint_res.error:
        log.error(f"  mint failed: {mint_res.error}")
        return

    # Extract tokenId + save
    if mint_res.receipt:
        from .bootstrap_pool2 import _extract_token_id
        new_token_id = _extract_token_id(mint_res.receipt, base.address)
        if new_token_id:
            log.info(f"  ✓ new LP tokenId: {new_token_id}")
            tracker.record_lp_mint(
                token_id=new_token_id,
                pool_label="ETH/USDC",
                hook=HOOK_ETH_USDC,
                tick_lower=TICK_LOWER,
                tick_upper=TICK_UPPER,
                ts=datetime.utcnow().isoformat(),
                block=mint_res.receipt.blockNumber,
                tx=mint_res.tx_hash,
                initial_amount0=a0,
                initial_amount1=a1,
            )

    log.info("\n" + "=" * 60)
    log.info("✓ Phase 2 REVERSE complete: HL → LP")
    log.info("=" * 60)


if __name__ == "__main__":
    run_reverse()
