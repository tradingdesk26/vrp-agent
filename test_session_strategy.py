"""Smoke test for session_strategy.decide(). No mocking — pure function."""
from src import session_strategy as ss
from src import state_machine as sm


def case(label, **kwargs):
    expected = kwargs.pop("expected")
    expected_route = kwargs.pop("expected_route", None)
    action = ss.decide(**kwargs)
    ok = (action.decision.value == expected
          and (expected_route is None or action.post_route == expected_route))
    icon = "✓" if ok else "✗"
    route = f" route={action.post_route}" if action.post_route else ""
    print(f"  {icon} {label:<55}  {action.decision.value}{route}  ({action.reason})")
    if not ok:
        print(f"    EXPECTED: {expected} route={expected_route}")
    return ok


# ─── Test cases ─────────────────────────────────────────────────
all_ok = True

print("\n=== Session entry/exit (VRP > 0 zone, normal day) ===")
all_ok &= case("PARKED_IN_LP at 19h, vrp=+10 → HOLD",
               state=sm.State.PARKED_IN_LP, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=19, today_session_done=False, entry_mode=None,
               expected="HOLD")
all_ok &= case("PARKED_IN_LP at 20h, vrp=+10, not done → ENTER_SESSION_LONG",
               state=sm.State.PARKED_IN_LP, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=20, today_session_done=False, entry_mode=None,
               expected="ENTER_SESSION_LONG")
all_ok &= case("LONG_ON_HL session at 21h, vrp=+10 → HOLD",
               state=sm.State.LONG_ON_HL, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=21, today_session_done=True, entry_mode="session",
               pnl_pct=0.02, expected="HOLD")
all_ok &= case("LONG_ON_HL session at 22h, vrp=+10 → EXIT_LONG to lp",
               state=sm.State.LONG_ON_HL, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=22, today_session_done=True, entry_mode="session",
               pnl_pct=0.02, expected="EXIT_LONG", expected_route="lp")
all_ok &= case("PARKED_IN_LP at 20h, today_done=True → HOLD",
               state=sm.State.PARKED_IN_LP, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=20, today_session_done=True, entry_mode=None,
               expected="HOLD")

print("\n=== Session entry/exit (VRP < 0 zone, bear day) ===")
all_ok &= case("CASH_ON_HL at 20h, vrp=-5 → ENTER_SESSION_LONG",
               state=sm.State.CASH_ON_HL, vrp_now=-5.0, vrp_prev=-5.0,
               hour_utc=20, today_session_done=False, entry_mode=None,
               expected="ENTER_SESSION_LONG")
all_ok &= case("LONG_ON_HL session at 22h, vrp=-5 → EXIT_LONG to cash",
               state=sm.State.LONG_ON_HL, vrp_now=-5.0, vrp_prev=-5.0,
               hour_utc=22, today_session_done=True, entry_mode="session",
               pnl_pct=0.01, expected="EXIT_LONG", expected_route="cash")

print("\n=== Stop-loss ===")
all_ok &= case("LONG_ON_HL pnl=-6% at 21h → EXIT_LONG (stop)",
               state=sm.State.LONG_ON_HL, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=21, today_session_done=True, entry_mode="session",
               pnl_pct=-0.06, expected="EXIT_LONG", expected_route="lp")
all_ok &= case("LONG_ON_HL pnl=-4% at 21h → HOLD (above stop)",
               state=sm.State.LONG_ON_HL, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=21, today_session_done=True, entry_mode="session",
               pnl_pct=-0.04, expected="HOLD")
all_ok &= case("LONG_ON_HL pnl=-7% at 21h vrp=-3 → EXIT_LONG to cash",
               state=sm.State.LONG_ON_HL, vrp_now=-3.0, vrp_prev=-3.0,
               hour_utc=21, today_session_done=True, entry_mode="session",
               pnl_pct=-0.07, expected="EXIT_LONG", expected_route="cash")

print("\n=== Persistent-long override (VRP cross 30) ===")
all_ok &= case("CASH_ON_HL at 14h, vrp 25→35 → ENTER_PERSISTENT_LONG",
               state=sm.State.CASH_ON_HL, vrp_now=35.0, vrp_prev=25.0,
               hour_utc=14, today_session_done=False, entry_mode=None,
               expected="ENTER_PERSISTENT_LONG")
all_ok &= case("PARKED_IN_LP at 11h, vrp 28→32 → ENTER_PERSISTENT_LONG",
               state=sm.State.PARKED_IN_LP, vrp_now=32.0, vrp_prev=28.0,
               hour_utc=11, today_session_done=False, entry_mode=None,
               expected="ENTER_PERSISTENT_LONG")
all_ok &= case("LONG_ON_HL session at 21h, vrp 25→35 → UPGRADE_TO_PERSISTENT",
               state=sm.State.LONG_ON_HL, vrp_now=35.0, vrp_prev=25.0,
               hour_utc=21, today_session_done=True, entry_mode="session",
               pnl_pct=0.04, expected="UPGRADE_TO_PERSISTENT")
all_ok &= case("LONG_ON_HL persistent at 22h, vrp=20 → HOLD (no exit)",
               state=sm.State.LONG_ON_HL, vrp_now=20.0, vrp_prev=20.0,
               hour_utc=22, today_session_done=True, entry_mode="persistent",
               pnl_pct=0.05, expected="HOLD")
all_ok &= case("LONG_ON_HL persistent at 4h, vrp 8→5 → EXIT_LONG to lp",
               state=sm.State.LONG_ON_HL, vrp_now=5.0, vrp_prev=8.0,
               hour_utc=4, today_session_done=False, entry_mode="persistent",
               pnl_pct=0.08, expected="EXIT_LONG", expected_route="lp")
all_ok &= case("LONG_ON_HL persistent at 4h, vrp 8→-2 → EXIT_LONG to cash",
               state=sm.State.LONG_ON_HL, vrp_now=-2.0, vrp_prev=8.0,
               hour_utc=4, today_session_done=False, entry_mode="persistent",
               pnl_pct=0.08, expected="EXIT_LONG", expected_route="cash")

print("\n=== Cross-zero defensive triggers (idle states) ===")
all_ok &= case("PARKED_IN_LP at 14h, vrp +5→-2 → MOVE_LP_TO_DEFENSIVE",
               state=sm.State.PARKED_IN_LP, vrp_now=-2.0, vrp_prev=5.0,
               hour_utc=14, today_session_done=False, entry_mode=None,
               expected="MOVE_LP_TO_DEFENSIVE")
all_ok &= case("DEFENSIVE_CASH at 11h, vrp -3→+4 → MOVE_DEFENSIVE_TO_LP",
               state=sm.State.DEFENSIVE_CASH, vrp_now=4.0, vrp_prev=-3.0,
               hour_utc=11, today_session_done=False, entry_mode=None,
               expected="MOVE_DEFENSIVE_TO_LP")
all_ok &= case("PARKED_IN_LP at 14h, vrp +5→+2 → HOLD (no cross)",
               state=sm.State.PARKED_IN_LP, vrp_now=2.0, vrp_prev=5.0,
               hour_utc=14, today_session_done=False, entry_mode=None,
               expected="HOLD")

print("\n=== Bootstrap (no prior VRP) ===")
all_ok &= case("CASH_ON_HL at 20h, vrp_prev=None → ENTER_SESSION_LONG",
               state=sm.State.CASH_ON_HL, vrp_now=8.0, vrp_prev=None,
               hour_utc=20, today_session_done=False, entry_mode=None,
               expected="ENTER_SESSION_LONG")
all_ok &= case("PARKED_IN_LP at 12h, vrp_prev=None, vrp=35 → HOLD",
               state=sm.State.PARKED_IN_LP, vrp_now=35.0, vrp_prev=None,
               hour_utc=12, today_session_done=False, entry_mode=None,
               expected="HOLD")

print("\n=== Edge: today_session_done from earlier today ===")
all_ok &= case("CASH_ON_HL at 22h, today done, no position → HOLD",
               state=sm.State.CASH_ON_HL, vrp_now=10.0, vrp_prev=10.0,
               hour_utc=22, today_session_done=True, entry_mode=None,
               expected="HOLD")

print()
print("=" * 60)
print("ALL TESTS PASSED ✓" if all_ok else "FAILURES — fix before deploy")
print("=" * 60)
