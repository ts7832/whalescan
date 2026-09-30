from whalescan.config import load_config
from whalescan.finder import confirmed_wallets
from whalescan.ledger import Ledger

NOW = 1_790_000_000
DAY = 86400
CFG = load_config()


def call(id_, wallet, kind="INSIDER"):
    return {"id": id_, "wallet": wallet, "kind": kind}


def settlement(call_id, return_pct, at, *, irregular=False):
    return {"call_id": call_id, "type": "SETTLEMENT", "at": at, "return_pct": return_pct, "irregular": irregular}


def test_a_settled_win_before_now_confirms_the_wallet(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", "0xw"))
    led.append_mark(settlement("a", 0.5, NOW - DAY))
    assert confirmed_wallets(led, NOW, CFG) == {"0xw"}


def test_a_win_that_has_not_settled_yet_does_not_confirm():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(d)
        led.append_call(call("a", "0xw"))  # no settlement mark at all: still open
        assert confirmed_wallets(led, NOW, CFG) == set()


def test_a_win_settling_at_or_after_now_does_not_confirm_yet(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", "0xw"))
    led.append_mark(settlement("a", 0.5, NOW))  # settles exactly at `now`: not yet knowable
    assert confirmed_wallets(led, NOW, CFG) == set()


def test_a_loss_does_not_confirm(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", "0xw"))
    led.append_mark(settlement("a", -0.4, NOW - DAY))
    assert confirmed_wallets(led, NOW, CFG) == set()


def test_an_irregular_win_does_not_confirm(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", "0xw"))
    led.append_mark(settlement("a", 1.0, NOW - DAY, irregular=True))
    assert confirmed_wallets(led, NOW, CFG) == set()


def test_a_sniper_win_does_not_confirm():
    # confirmation is spec-limited to INSIDER / NEAR_MISS / INFORMED calls — a sniper's historical-edge win is
    # a different kind of evidence entirely.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        led = Ledger(d)
        led.append_call(call("a", "0xw", kind="SNIPER"))
        led.append_mark(settlement("a", 0.5, NOW - DAY))
        assert confirmed_wallets(led, NOW, CFG) == set()


def test_near_miss_and_informed_wins_also_confirm(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("a", "0xnear", kind="NEAR_MISS"))
    led.append_mark(settlement("a", 0.2, NOW - DAY))
    led.append_call(call("b", "0xinformed", kind="INFORMED"))
    led.append_mark(settlement("b", 0.2, NOW - DAY))
    assert confirmed_wallets(led, NOW, CFG) == {"0xnear", "0xinformed"}


def test_demotion_after_enough_losing_confirmed_calls(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("win", "0xw"))
    led.append_mark(settlement("win", 0.5, NOW - 100 * DAY))
    n = CFG.finder.demote_after
    for i in range(n):
        led.append_call(call(f"c{i}", "0xw", kind="CONFIRMED"))
        led.append_mark(settlement(f"c{i}", -0.2, NOW - (90 - i) * DAY))
    assert confirmed_wallets(led, NOW, CFG) == set()


def test_not_yet_demoted_below_the_threshold(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("win", "0xw"))
    led.append_mark(settlement("win", 0.5, NOW - 100 * DAY))
    n = CFG.finder.demote_after - 1
    for i in range(n):
        led.append_call(call(f"c{i}", "0xw", kind="CONFIRMED"))
        led.append_mark(settlement(f"c{i}", -0.2, NOW - (90 - i) * DAY))
    assert confirmed_wallets(led, NOW, CFG) == {"0xw"}


def test_a_wallet_stays_confirmed_if_its_confirmed_calls_average_a_profit(tmp_path):
    led = Ledger(tmp_path)
    led.append_call(call("win", "0xw"))
    led.append_mark(settlement("win", 0.5, NOW - 100 * DAY))
    n = CFG.finder.demote_after
    for i in range(n):
        led.append_call(call(f"c{i}", "0xw", kind="CONFIRMED"))
        led.append_mark(settlement(f"c{i}", 0.3, NOW - (90 - i) * DAY))  # winning, not losing
    assert confirmed_wallets(led, NOW, CFG) == {"0xw"}
