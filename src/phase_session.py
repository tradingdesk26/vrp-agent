"""
Session-strategy orchestrators.

Wraps existing pipelines (phase2_forward, phase2_reverse, phase_defensive,
transitions) into clean session-aware entry/exit functions.

  enter_long(state, mode, session_date)
    Dispatches based on current state:
      CASH_ON_HL      → hl_exec.open_long()            (~3s)
      PARKED_IN_LP    → phase2_forward.run_forward()   (~60s)
      DEFENSIVE_CASH  → phase2_forward.run_forward_from_cash()  (~45s)

  exit_long(target)
    target='cash' → hl_exec.close_position(), capital stays on HL
    target='lp'   → phase2_reverse.run_reverse(), full pipeline back to LP
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import config
from . import phase2_forward
from . import phase2_reverse
from . import state_machine as sm
from . import transitions
from .hl_executor import HLExecutor
from .on_chain.client import BaseClient
from .on_chain.lp_manager import LPManager
from .pnl_tracker import PnLTracker

log = logging.getLogger(__name__)


def enter_long(
    state: sm.State,
    mode: str,
    session_date: str | None,
    entry_reason: str,
) -> bool:
    """Open long position, routing via current state. Returns True on success."""
    if state == sm.State.CASH_ON_HL:
        log.info(f"  ▶ enter_long path: CASH_ON_HL → LONG_ON_HL (direct)")
        hl_exec = HLExecutor()
        tracker = PnLTracker()
        base    = BaseClient()
        lp_mgr  = LPManager(base)
        snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
        ok, err = transitions.enter_long_hl(hl_exec, tracker, snap, entry_reason)
        if ok and not config.RISK.dry_run:
            # Backfill entry_mode + session_date on the open trade
            tid = tracker.open_trade_id()
            if tid:
                tracker._conn.execute(
                    "UPDATE trades SET entry_mode=?, session_date=? WHERE id=?",
                    (mode, session_date, tid),
                )
                tracker._conn.commit()
        log.info(f"  enter_long → {'OK' if ok else 'FAIL: ' + (err or '')}")
        return ok

    if state == sm.State.PARKED_IN_LP:
        log.info(f"  ▶ enter_long path: PARKED_IN_LP → LONG_ON_HL (phase2_forward)")
        try:
            phase2_forward.run_forward(
                entry_mode=mode,
                session_date=session_date,
                entry_reason=entry_reason,
            )
            return True
        except Exception:
            log.exception("  ✗ phase2_forward FAILED")
            return False

    if state == sm.State.DEFENSIVE_CASH:
        log.info(f"  ▶ enter_long path: DEFENSIVE_CASH → LONG_ON_HL (forward_from_cash)")
        try:
            phase2_forward.run_forward_from_cash(
                entry_mode=mode,
                session_date=session_date,
                entry_reason=entry_reason,
            )
            return True
        except Exception:
            log.exception("  ✗ phase2_forward_from_cash FAILED")
            return False

    if state == sm.State.IN_TRANSIT_TO_HL:
        # USDC stuck on HyperEVM (mid-bridge from Base). Push it through
        # CoreDepositWallet.deposit → HL PERPS, then re-dispatch.
        log.info(f"  ▶ enter_long path: IN_TRANSIT_TO_HL → CASH_ON_HL → LONG_ON_HL (auto-recover)")
        try:
            _resume_deposit_to_hl()
        except Exception:
            log.exception("  ✗ auto-recover deposit FAILED")
            return False
        # Recurse with re-snapshot. State should now be CASH_ON_HL.
        return enter_long(sm.State.CASH_ON_HL, mode, session_date, entry_reason)

    if state == sm.State.UNKNOWN:
        # Best-effort recovery: read HyperEVM, push to HL if anything's there,
        # then retry from CASH_ON_HL.
        log.warning(f"  ▶ enter_long path: UNKNOWN — attempting recovery diagnostic")
        try:
            from .phase2_forward import hyperevm_usdc_balance
            base = BaseClient()
            evm_bal = hyperevm_usdc_balance(base.address)
            log.warning(f"    HyperEVM USDC: ${evm_bal/1e6:.4f}")
            if evm_bal > 1_000_000:
                log.warning(f"    → pushing to HL via deposit")
                _resume_deposit_to_hl()
                return enter_long(sm.State.CASH_ON_HL, mode, session_date, entry_reason)
        except Exception:
            log.exception("  ✗ UNKNOWN recovery FAILED")
            return False
        log.error(f"    no recoverable funds detected — manual intervention needed")
        return False

    log.error(f"  ✗ enter_long: unsupported state {state.value}")
    return False


def _resume_deposit_to_hl() -> None:
    """Deposit any HyperEVM USDC → HL PERPS. Idempotent (skips if < $1)."""
    import time
    from eth_account import Account
    from . import config
    from .phase2_forward import (
        USDC_HYPEREVM, CORE_DEPOSIT_WALLET, CORE_DEPOSIT_ABI,
        HYPEREVM_CHAIN_ID, DEST_PERPS, ERC20_ABI, hyperevm_w3,
    )

    w3 = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    addr = account.address
    usdc = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    cdw  = w3.eth.contract(address=CORE_DEPOSIT_WALLET, abi=CORE_DEPOSIT_ABI)
    bal = usdc.functions.balanceOf(addr).call()
    log.info(f"    HyperEVM USDC balance: ${bal/1e6:.4f}")
    if bal < 1_000_000:
        log.info(f"    nothing to deposit (<$1)")
        return
    nonce = w3.eth.get_transaction_count(addr)
    def send(fn, gas_limit=200_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from": addr, "nonce": nonce, "chainId": HYPEREVM_CHAIN_ID,
            "gas": gas_limit,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"      submitted: {h.hex()}")
        r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"      confirmed: status={r.status}")
        return r
    cur_allow = usdc.functions.allowance(addr, CORE_DEPOSIT_WALLET).call()
    if cur_allow < bal:
        send(usdc.functions.approve(CORE_DEPOSIT_WALLET, 2**256 - 1), gas_limit=100_000)
        time.sleep(2)
    send(cdw.functions.deposit(bal, DEST_PERPS), gas_limit=200_000)
    # Wait for HL credit
    log.info(f"    polling HL credit (60s)...")
    from .hl_executor import HLExecutor
    hl = HLExecutor()
    start = time.time()
    pre = hl.get_usdc_balance()
    while time.time() - start < 60:
        cur = hl.get_usdc_balance()
        if cur > pre + (bal / 1e6) * 0.95:
            log.info(f"    HL credit detected: margin=${cur:.4f}")
            return
        time.sleep(3)
    log.warning(f"    HL credit not detected within 60s (deposit submitted, may settle later)")


def _resume_burn_to_base() -> bool:
    """CCTP burn HyperEVM USDC → Base. Idempotent (skips if < $1).
    Returns True if Base USDC arrived. After this, agent will be in
    DEFENSIVE_CASH state; strategy decides whether to re-LP."""
    import time
    from eth_account import Account
    from . import config
    from .on_chain import cctp
    from .on_chain.client import BaseClient, USDC_BASE
    from .on_chain.cctp import (
        TOKEN_MESSENGER_V2, TOKEN_MESSENGER_V2_ABI,
        DOMAIN_BASE, DOMAIN_HYPEREVM, FINALITY_FAST,
        MESSAGE_TRANSMITTER_V2, address_to_bytes32,
    )
    from .phase2_forward import (
        USDC_HYPEREVM, HYPEREVM_CHAIN_ID, ERC20_ABI, hyperevm_w3,
    )

    w3 = hyperevm_w3()
    account = Account.from_key(config.HL.private_key)
    addr = account.address
    base = BaseClient()
    usdc_evm = w3.eth.contract(address=USDC_HYPEREVM, abi=ERC20_ABI)
    tm_evm   = w3.eth.contract(address=TOKEN_MESSENGER_V2, abi=TOKEN_MESSENGER_V2_ABI)

    bal = usdc_evm.functions.balanceOf(addr).call()
    log.info(f"    HyperEVM USDC balance: ${bal/1e6:.4f}")
    if bal < 1_000_000:
        log.info(f"    nothing to burn (<$1)")
        return False

    pre_base_usdc = base.balance(USDC_BASE)
    nonce = w3.eth.get_transaction_count(addr)

    def send(fn, gas_limit=300_000):
        nonlocal nonce
        tx = fn.build_transaction({
            "from": addr, "nonce": nonce, "chainId": HYPEREVM_CHAIN_ID,
            "gas": gas_limit,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": int(0.001e9),
        })
        signed = account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        nonce += 1
        log.info(f"      submitted: {h.hex()}")
        r = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        log.info(f"      confirmed: status={r.status}")
        return r

    # Approve TokenMessengerV2 on HyperEVM
    cur_allow = usdc_evm.functions.allowance(addr, TOKEN_MESSENGER_V2).call()
    if cur_allow < bal:
        log.info("    approving USDC → TokenMessengerV2 on HyperEVM")
        send(usdc_evm.functions.approve(TOKEN_MESSENGER_V2, 2**256 - 1),
             gas_limit=100_000)
        time.sleep(2)

    # depositForBurn → Base
    max_fee = bal * 50 // 10_000  # 0.5%
    mint_recipient = address_to_bytes32(addr)
    dest_caller = bytes(32)
    log.info(f"    depositForBurn: amount={bal}, dest=Base(6)")
    burn_receipt = send(
        tm_evm.functions.depositForBurn(
            bal, DOMAIN_BASE, mint_recipient, USDC_HYPEREVM,
            dest_caller, max_fee, FINALITY_FAST,
        ),
        gas_limit=200_000,
    )
    burn_tx = burn_receipt.transactionHash.hex()
    if not burn_tx.startswith("0x"):
        burn_tx = "0x" + burn_tx

    # Wait Iris attestation
    log.info(f"    waiting Iris attestation (HyperEVM source)...")
    attestation = cctp.wait_for_attestation(
        burn_tx, src_domain=DOMAIN_HYPEREVM,
        max_wait_sec=300, poll_interval_sec=5,
    )
    if not attestation:
        log.error("    attestation timeout — burn submitted but mint pending")
        return False

    # Wait Base mint
    log.info(f"    polling Base USDC arrival (180s)...")
    start = time.time()
    while time.time() - start < 180:
        cur = base.balance(USDC_BASE)
        if cur >= pre_base_usdc + bal * 0.95:
            log.info(f"    Base USDC arrived: ${cur/1e6:.4f}")
            return True
        time.sleep(5)
    log.warning(f"    Base USDC not detected within 180s — Circle relayer slow, will retry next tick")
    return False


def exit_long(target: str, exit_reason: str) -> bool:
    """Close long position and route capital.

    Args:
        target: 'cash' (stay on HL as CASH_ON_HL) or 'lp' (full reverse to PARKED_IN_LP)
        exit_reason: text reason for SQLite
    """
    if target == "cash":
        log.info(f"  ▶ exit_long path: LONG_ON_HL → CASH_ON_HL (direct close)")
        hl_exec = HLExecutor()
        tracker = PnLTracker()
        base    = BaseClient()
        lp_mgr  = LPManager(base)
        snap = transitions.snapshot(base, lp_mgr, hl_exec, tracker)
        ok, err = transitions.exit_long_hl(hl_exec, tracker, snap, exit_reason)
        log.info(f"  exit_long → {'OK' if ok else 'FAIL: ' + (err or '')}")
        return ok

    if target == "lp":
        log.info(f"  ▶ exit_long path: LONG_ON_HL → PARKED_IN_LP (phase2_reverse)")
        try:
            phase2_reverse.run_reverse()
            return True
        except Exception:
            log.exception("  ✗ phase2_reverse FAILED")
            return False

    log.error(f"  ✗ exit_long: unknown target '{target}'")
    return False
