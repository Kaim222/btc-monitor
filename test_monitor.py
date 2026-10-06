"""Offline reliability checks; all temporary files stay inside this clone."""
import ast
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

import mstr_gap as monitor
from merge_ledger import merge

ROOT = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network is forbidden in monitor tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(monitor.urllib.request, "urlopen", blocked)


@pytest.fixture
def scenario(monkeypatch):
    with tempfile.TemporaryDirectory(prefix=".monitor-test-", dir=ROOT) as folder:
        work = Path(folder).resolve()
        assert work.is_relative_to(ROOT)
        state_path, ledger_path = work / "state.json", work / "ledger.json"
        state_path.write_text(json.dumps({"strc": 98.5}))
        ledger_path.write_text("[]")
        monkeypatch.setattr(monitor, "STATE_FILE", str(state_path))
        monkeypatch.setattr(monitor, "LEDGER_FILE", str(ledger_path))
        monkeypatch.setattr(monitor, "FORCE", True)
        monkeypatch.setattr(monitor, "TEST", False)
        monkeypatch.setattr(monitor, "load_config", lambda: {"_source": "test"})
        monkeypatch.setattr(monitor, "strategy_holdings", lambda state: (845050, 450.112, "stub"))
        monkeypatch.setattr(monitor, "sync_pine", lambda *args: False)
        monkeypatch.setattr(monitor, "target", lambda *args: 100 / (100000 * monitor.BPS))
        index = pd.date_range("2026-09-18 09:30", periods=100, freq="min", tz=monitor.NY)
        mstr = pd.DataFrame({"Close": 100.0, "Low": 100.0}, index=index)
        mstr.iloc[-1, 1] = 90.0  # A due Lag followed by a due Rich.
        btc = pd.Series(100000.0, index=pd.date_range("2026-09-18 08:00", periods=200, freq="min", tz=monitor.NY))
        mstx = pd.Series(100.0, index=index)
        mstx.iloc[-1] = 121.0
        mstr.iloc[-1, 0] = 110.5  # Rich uses twice MSTR excess, not the fund tracking gap.
        data = {"MSTR": mstr, "BTC-USD": btc, "MSTX": mstx}
        monkeypatch.setattr(monitor, "bars", lambda ticker, **kwargs: data[ticker].copy())
        daily_index = pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=1), periods=130)
        daily = pd.DataFrame({"Close": 90000.0}, index=daily_index)
        daily.iloc[-1, 0] = 100000.0
        monkeypatch.setattr(monitor.yf, "Ticker", lambda ticker: SimpleNamespace(
            history=lambda **kwargs: daily.copy() if ticker == "BTC-USD" else pd.DataFrame({"Close": []})))
        sent = []
        def send(title, message, **kwargs):
            sent.append((title, message, kwargs))
            return True
        monkeypatch.setattr(monitor, "send_pushover", send)
        yield SimpleNamespace(state=state_path, ledger=ledger_path, data=data, sent=sent, send=send, index=index)


def test_lag_exception_does_not_stop_rich_or_saves(scenario, monkeypatch, capsys):
    def send(title, message, **kwargs):
        if title.startswith("Lag "):
            raise TypeError("injected Lag formatting failure")
        return scenario.send(title, message, **kwargs)
    monkeypatch.setattr(monitor, "send_pushover", send)
    assert monitor.main() == 1
    assert any(title.startswith("Rich ") for title, _, _ in scenario.sent)
    state = json.loads(scenario.state.read_text())
    ledger = json.loads(scenario.ledger.read_text())
    assert state["last_bar"] == scenario.index[-1].isoformat()
    assert state["fired"] == ["rich"]
    assert [row["kind"] for row in ledger] == ["rich"]
    errors = [message for title, message, _ in scenario.sent if title == "Monitor error"]
    assert len(errors) == 1 and "Lag: injected Lag formatting failure" in errors[0]
    assert "Traceback" in capsys.readouterr().err


def test_all_percent_format_argument_counts():
    tree = ast.parse((ROOT / "mstr_gap.py").read_text(encoding="utf-8"))
    conversion = re.compile(r"%(?:%|(?:\([^)]*\))?[#0 +\-]*(?:\d+|\*)?(?:\.(?:\d+|\*))?[hlL]?[diouxXeEfFgGcrsa])")
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mod):
            continue
        assert isinstance(node.left, ast.Constant) and isinstance(node.left.value, str), node.lineno
        fmt = node.left.value
        matches = list(conversion.finditer(fmt))
        assert "%" not in conversion.sub("", fmt), (node.lineno, fmt)
        expected = sum(1 + match.group().count("*") for match in matches if match.group() != "%%")
        actual = len(node.right.elts) if isinstance(node.right, ast.Tuple) else 1
        assert expected == actual, (node.lineno, fmt, expected, actual)
        checked += 1
    assert checked > 30


def test_send_pushover_retries_and_returns_false(monkeypatch):
    monkeypatch.setenv("PUSHOVER_TOKEN", "test-token")
    monkeypatch.setenv("PUSHOVER_USER", "test-user")
    http = Mock(side_effect=OSError("HTTP unavailable"))
    sleep = Mock()
    monkeypatch.setattr(monitor.urllib.request, "urlopen", http)
    monkeypatch.setattr(monitor.time, "sleep", sleep)
    assert monitor.send_pushover("test", "test", sound="siren", priority=1) is False
    assert http.call_count == 3
    assert sleep.call_count == 2
    payload = monitor.urllib.parse.parse_qs(http.call_args.args[0].data.decode())
    assert payload["priority"] == ["1"] and payload["sound"] == ["siren"]


def test_holdings_failure_does_not_stop_market(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "strategy_holdings", Mock(side_effect=ValueError("bad holdings")))
    assert monitor.main() == 1
    assert any(title.startswith("Rich ") for title, _, _ in scenario.sent)
    assert "holdings: bad holdings" in next(msg for title, msg, _ in scenario.sent if title == "Monitor error")


def test_empty_strc_uses_cache_and_priorities(scenario):
    assert monitor.main() == 0
    state = json.loads(scenario.state.read_text())
    assert state["strc"] == 98.5
    lag = next(kwargs for title, _, kwargs in scenario.sent if title.startswith("Lag "))
    assert lag == {"sound": "siren", "priority": 1}


def test_test_push_uses_lag_delivery_path(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "TEST", True)
    assert monitor.main() == 0
    assert scenario.sent[-1][0] == "Test"
    assert scenario.sent[-1][2] == {"sound": "siren", "priority": 1}


def test_btc_floor_before_1030_uses_overnight_series(scenario):
    scenario.data["MSTR"] = scenario.data["MSTR"].iloc[:40].copy()
    scenario.data["MSTR"].iloc[-1, 1] = 90.0
    scenario.data["MSTX"] = scenario.data["MSTX"].iloc[:40]
    btc = scenario.data["BTC-USD"]
    btc.loc[btc.index < scenario.index[0]] = 110000.0
    assert monitor.main() == 0
    assert not any(title.startswith("Lag ") for title, _, _ in scenario.sent)
    assert any(row["kind"] == "watch" for row in json.loads(scenario.ledger.read_text()))


def test_long_gap_scans_90_bars(scenario):
    scenario.data["MSTR"].loc[:, "Low"] = 100.0
    scenario.data["MSTR"].iloc[40, 1] = 90.0
    scenario.state.write_text(json.dumps({"strc": 98.5, "last_bar": scenario.index[0].isoformat()}))
    assert monitor.main() == 0
    lag = next(row for row in json.loads(scenario.ledger.read_text()) if row["kind"] == "lag")
    assert lag["time"] == scenario.index[40].isoformat()


def test_merge_fills_null_and_keeps_first_muted():
    first = {"kind": "lag", "time": "x", "muted": False, "score": None}
    second = {"kind": "lag", "time": "x", "muted": True, "score": 2}
    assert merge([first], [second]) == [{**first, "score": 2}]


def test_failed_delivery_and_error_summary_still_save(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "send_pushover", Mock(return_value=False))
    assert monitor.main() == 1
    calls = monitor.send_pushover.call_args_list
    assert sum(call.args[0] == "Monitor error" for call in calls) == 1
    summary = calls[-1].args[1]
    assert "Lag:" in summary and "Rich:" in summary
    assert json.loads(scenario.state.read_text())["fired"] == []
    assert json.loads(scenario.ledger.read_text())[0]["kind"] == "watch"


def test_holdings_priority(scenario):
    scenario.state.write_text(json.dumps({"strc": 98.5, "strategy_last": {"btc_held": 800000, "shares_m": 450.112}}))
    assert monitor.main() == 0
    kwargs = next(kwargs for title, _, kwargs in scenario.sent if title == "Holdings changed")
    assert kwargs == {"sound": "magic", "priority": -1}


def test_ladder_lines_and_plays():
    assert monitor.LADDER_LINES == (10, 60, 75)
    assert [monitor.ladder_band(q) for q in (9.9, 10, 59.9, 60, 74.9, 75, 99)] == ["MSTX", "MSTR", "MSTR", "IBIT", "IBIT", "sell zone", "sell zone"]
    assert "10 line" in monitor.BAND_PLAY["MSTX"] and "60 line" in monitor.BAND_PLAY["MSTR"] and "75 line" in monitor.BAND_PLAY["IBIT"]
    assert monitor.BAND_LINE == {"MSTX": 10, "MSTR": 60, "IBIT": 75}


def _month_end_case():
    """The fixture's month-end close, the band it reads on today's lines, and a different band to pretend was stored."""
    today = pd.Timestamp.now(tz="UTC").normalize(); first = today.replace(day=1)
    m_end = (first - pd.Timedelta(milliseconds=1)).to_pydatetime(); px = 100000.0 if today.day == 1 else 90000.0
    expected = monitor.ladder_band(monitor.ladder_q(px, m_end))
    return (first - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), expected, ("IBIT" if expected != "IBIT" else "MSTR")


def test_moved_lines_are_adopted_with_one_notice_and_no_crossing_push(scenario):
    """State written under other lines, from this same monthly close: one plain notice, never a crossing push."""
    close, expected, stored = _month_end_case()
    scenario.state.write_text(json.dumps({"strc": 98.5, "band": stored, "band_q": 10.6, "band_close": close}))
    monitor.main()
    titles = [x[0] for x in scenario.sent]
    assert titles.count("Ladder lines: 10 / 60 / 75") == 1 and not any(x.startswith("Ladder band") for x in titles)
    notice = next(x[1] for x in scenario.sent if x[0] == "Ladder lines: 10 / 60 / 75")
    assert "No monthly close crossed a line" in notice and "<b>%s</b>" % expected in notice and "MSTR 10 to 60" in notice
    state = json.loads(scenario.state.read_text())
    assert state["band"] == expected and state["band_lines"] == "10 / 60 / 75" and "band_changed" in state
    before = len(scenario.sent); monitor.main()
    assert not any(x[0].startswith("Ladder") for x in scenario.sent[before:])


def test_fresh_state_takes_the_band_silently(scenario):
    close, expected, stored = _month_end_case()
    monitor.main()
    assert not any(x[0].startswith("Ladder") for x in scenario.sent)
    state = json.loads(scenario.state.read_text())
    assert state["band"] == expected and state["band_lines"] == "10 / 60 / 75"


def test_a_new_monthly_close_still_pushes_the_crossing(scenario):
    """Stored band from an older close: the normal rotation push fires, with or without the lines stamp, never the notice."""
    close, expected, stored = _month_end_case()
    for extra in ({"band_lines": "10 / 60 / 75"}, {}):
        scenario.sent.clear()
        scenario.state.write_text(json.dumps(dict({"strc": 98.5, "band": stored, "band_q": 9.0, "band_close": "2026-01-31"}, **extra)))
        monitor.main()
        titles = [x[0] for x in scenario.sent]
        assert "Ladder band: %s" % expected in titles and not any(x.startswith("Ladder lines") for x in titles)


def test_ladder_model_matches_the_site():
    """Pinned to the site's own priceToQuantile and quantileToPrice under model v2 (checked in the browser on 2026-09-20)."""
    from datetime import datetime, timezone
    noon = lambda y, m, d: datetime(y, m, d, 12, tzinfo=timezone.utc)
    for price, when, want in ((77200.0078125, noon(2026, 9, 12), 9.786), (80400, noon(2026, 9, 20), 11.359), (58600, noon(2026, 6, 30), 0.110),
                              (124800, noon(2025, 10, 6), 82.866), (15800, noon(2022, 11, 21), 4.713), (250000, noon(2028, 1, 21), 87.199)):
        assert abs(monitor.ladder_q(price, when) - want) < 0.01, (price, when, monitor.ladder_q(price, when))
    for q, want in ((10, 85843), (60, 144899), (75, 166029), (85, 181801)):
        assert abs(monitor.ladder_price(q, noon(2026, 12, 31)) - want) < 2, (q, monitor.ladder_price(q, noon(2026, 12, 31)))
    offs = [o for _, o in monitor._band_offsets(noon(2040, 1, 1))]
    assert offs == sorted(offs, reverse=True), "the lines must never cross"
    import math
    lo, hi = monitor.ladder_price(0.01, noon(2026, 12, 31)), monitor.ladder_price(99.99, noon(2026, 12, 31))
    assert math.isfinite(lo) and math.isfinite(hi) and lo < monitor.ladder_price(0.1, noon(2026, 12, 31)) < monitor.ladder_price(99.9, noon(2026, 12, 31)) < hi, "tails extrapolate like the site"


@pytest.mark.parametrize("config, cheap, rich", [({}, -0.17, 0.20), ({"gap_centre": 0}, -0.17, 0.20), ({"gap_centre": 0.04}, -0.17, 0.20)])
def test_gap_centre_thresholds_leave_lag_unchanged(config, cheap, rich):
    monitor.configure(config)
    assert monitor.CHEAP_X == pytest.approx(cheap)
    assert monitor.RICH_X == pytest.approx(rich)
    assert monitor.LAG_X == pytest.approx(-0.03)
    assert monitor.LAG_WATCH == pytest.approx(-0.01)
    assert monitor.LAG_SLOPE == pytest.approx(0.0125)


@pytest.mark.parametrize("config, mstx, expected", [
    ({}, 82.9, "cheap"), ({}, 83.1, None),
    ({}, 119.9, None), ({}, 120.1, "rich"),
    ({"gap_centre": 0.04}, 83.1, None), ({"gap_centre": 0.04}, 120.1, "rich"),
    ({"cheap_threshold": -0.05, "rich_threshold": 0.075}, 89.9, "cheap"),
    ({"cheap_threshold": -0.05, "rich_threshold": 0.075}, 115.1, "rich"),
])
def test_centred_and_legacy_alerts(scenario, monkeypatch, config, mstx, expected):
    monkeypatch.setattr(monitor, "load_config", lambda: {"_source": "test", **config})
    scenario.data["MSTX"].iloc[-1] = mstx
    scenario.data["MSTR"].iloc[-1, 0] = 100*(1+(mstx/100-1)/2)
    assert monitor.main() == 0
    kinds = [row["kind"] for row in json.loads(scenario.ledger.read_text())]
    assert "lag" in kinds
    assert [kind for kind in kinds if kind in ("cheap", "rich")] == ([expected] if expected else [])


def test_fitted_price_shortfall_cap_and_independent_lag():
    config = json.loads((ROOT / "mstr_config.json").read_text())
    monitor.configure(config)
    f = config["fit"]
    for btc in (60000, 81526.74, 100000, 125000, 150000, 200000):
        for strc in (80, 98.7, 100, 105):
            expected = min(2, f["a"] + f["b"] * (btc - 75000) / 2500 + f["c"] * max(0, 100-strc))
            assert monitor.target(strc, btc) == pytest.approx(expected)
    assert monitor.target(100, 100000) == monitor.target(105, 100000)
    assert monitor.target(80, 100000) <= monitor.target(100, 100000)
    assert monitor.target(98.7, 200000) == 2
    assert monitor.target(98.7, 100000, monitor.LAG_SLOPE) == pytest.approx(1.025)
    assert monitor.target(90, 100000, monitor.LAG_SLOPE) == pytest.approx(0.9625)


def test_old_remote_config_uses_local_fit(monkeypatch):
    from io import BytesIO
    monkeypatch.setattr(monitor.urllib.request, "urlopen", lambda *a, **k: BytesIO(b'{"btc_slope_per_2500": 0.025}'))
    monkeypatch.setattr(monitor, "CONFIG_FILE", str(ROOT / "mstr_config.json"))
    cfg = monitor.load_config()
    assert cfg["_source"] == "local"
    assert cfg["fit"]["c"] <= 0


def test_pine_sync_fits_prices_preserves_lag_and_newlines(tmp_path, monkeypatch):
    repo_config = json.loads((ROOT / "mstr_config.json").read_text())
    # lag and gate settings deliberately unlike the Pine defaults, so a missing regex leaves the old value and fails
    config = {**repo_config, "lag_threshold": -0.02, "btc_hour_move_floor": -0.005, "lag_slope": 0.015,
              "regime_gate": False, "rich_gate": True}
    paths = []
    for name in monitor.PINE_FILES:
        original = (ROOT / name).read_bytes()
        for already in (b'input.bool(false, "Trend gate', b'input.bool(true, "Gate the sell too',
                        b'input.float(0.015, "Lag slope', b'input.float(-0.5, "BTC must have held',
                        b'input.float(-2, "Lag alert', b'input.float(-4, "Lag alert'):
            assert already not in original   # the sync must be what puts these values there
        path = tmp_path / name
        path.write_bytes(original)
        paths.append(str(path))
    monkeypatch.setattr(monitor, "PINE_FILES", paths)
    monitor.configure(config)
    try:
        assert monitor.sync_pine(900000, 500)
        for path in paths:
            raw = Path(path).read_bytes()
            assert raw.count(b"\n") == raw.count(b"\r\n")
            text = raw.decode()
            assert 'input.float(%s, "Fitted intercept"' % repr(config["fit"]["a"]) in text
            assert 'input.float(%s, "Target mNAV slope' % repr(config["fit"]["b"]) in text
            assert 'target = (sheetBase ? math.min(2.0, lagBase(strc) + slope * (btc - 75000) / 2500) : math.min(2.0, fitA + fitC * math.max(0, fitPar - strc)' in text
            assert 'input.bool(false, "Sheet mNAV base from STRC"' in text   # the repo config is a fitted line, so the sheet switch syncs off
            # every indicator runs the monitor's lag: its own slope, its line and the BTC floor, all from the config
            assert 'input.float(0.015, "Lag slope per $2,500 of BTC (the lag runs on its own)"' in text
            assert 'input.float(-0.5, "BTC must have held (% over the window, floor)"' in text
            # and the two gate switches the monitor reads
            assert 'input.bool(false, "Trend gate' in text
            assert 'input.bool(true, "Gate the sell too' in text
            assert "Legacy" not in text
            if Path(path).name == "mstx_projected.pine":
                assert 'input.float(-4, "Lag alert, MSTX % vs the trailing window"' in text
                assert 'input.float(%g, "Cheap line, MSTX' % (200*config['cheap_threshold']) in text
                assert 'input.float(%g, "Rich line, MSTX' % (200*config['rich_threshold']) in text
            else:
                assert 'input.float(-2, "Lag alert (% vs the trailing window)"' in text
                assert 'input.float(%g, "Cheap line' % (100*config['cheap_threshold']) in text
                assert 'input.float(%g, "Rich line' % (100*config['rich_threshold']) in text
            assert 'lagBase(strc) + lagSlope * (btc - 75000) / 2500' in text
        assert not monitor.sync_pine(900000, 500)
    finally:
        monitor.configure(repo_config)


@pytest.fixture
def premium_value():
    return {"n": 10, "average": -.04, "from": "2026-09-04", "to": "2026-09-18",
            "p10": -.065, "p25": -.044, "p75": .027, "p90": .06}


def test_premium_fetch_once_saves_and_falls_back(monkeypatch, premium_value):
    from io import BytesIO
    state={}
    fetch=Mock(return_value=BytesIO(json.dumps({"premium":premium_value}).encode()))
    monkeypatch.setattr(monitor.urllib.request,"urlopen",fetch)
    assert monitor.load_premium(state)==premium_value
    fetch.assert_called_once()
    assert fetch.call_args.kwargs['timeout']==4
    assert fetch.call_args.args[0].full_url==monitor.MODEL_URL
    assert state['premium_last']==premium_value
    fetch.side_effect=TimeoutError('offline')
    assert monitor.load_premium(state)==premium_value
    assert monitor.load_premium({})['average']==0


@pytest.mark.parametrize('bad', [None, {}, {"average":float('nan')}, {"n":0}, {"p75":-.10}])
def test_invalid_premium_keeps_saved(monkeypatch,premium_value,bad):
    from io import BytesIO
    payload=None if bad is None else {**premium_value,**bad}
    if bad=={}: payload={}
    monkeypatch.setattr(monitor.urllib.request,'urlopen',lambda *a,**k:BytesIO(json.dumps({'premium':payload}).encode()))
    state={'premium_last':premium_value.copy()}
    assert monitor.load_premium(state)==premium_value
    assert state['premium_last']==premium_value


@pytest.mark.parametrize('excess,kind',[(-.044001,'cheap'),(-.043999,None),(.026999,None),(.027001,'rich')])
def test_alerts_use_additive_excess_and_twice_mstr_lines(scenario,monkeypatch,premium_value,excess,kind):
    fetch=Mock(return_value=premium_value)
    monkeypatch.setattr(monitor,'load_premium',fetch)
    scenario.data['MSTR'].iloc[-1,0]=100*(1+premium_value['average']+excess)
    scenario.data['MSTX'].iloc[-1]=500  # Tracking difference cannot trip an excess alert.
    assert monitor.main()==0
    fetch.assert_called_once()
    rows=json.loads(scenario.ledger.read_text())
    swings=[r for r in rows if r['kind'] in ('cheap','rich')]
    assert [r['kind'] for r in swings]==([kind] if kind else [])
    assert monitor.CHEAP_X==pytest.approx(2*premium_value['p25'])
    assert monitor.RICH_X==pytest.approx(2*premium_value['p75'])
    if swings:
        assert swings[0]['gap_mstx']==pytest.approx(round(excess*200,2))
        assert swings[0]['proj']==96
    assert monitor.LAG_SLOPE==.0125
    assert monitor.LAG_X==-.03


def test_pine_lag_rules_match_the_monitor():
    """The three indicators judge the lag where and when the monitor does (the gaps found on 2026-10-01)."""
    src = (ROOT / "mstr_gap.py").read_text()
    config = json.loads((ROOT / "mstr_config.json").read_text())
    assert 'rolling(60, min_periods=30)' in src and 'timedelta(minutes=60)' in src
    for name in monitor.PINE_FILES:
        text = (ROOT / name).read_text()
        assert 'ratioLo = mstrLo / (btc * tgtLag)' in text and 'request.security(mstrSym, timeframe.period, low)' in text   # at MSTR's bar low
        assert 'input.float(%g, "Lag slope per $2,500 of BTC (the lag runs on its own)"' % config["lag_slope"] in text
        assert 'input.int(30, "Minutes after the open before the lag can fire"' in text                                       # min_periods 30
        assert 'request.security(btcSym, timeframe.period, close[winBars])' in text and 'btcMove = (btc / btcAgo - 1) * 100' in text   # BTC's own hour
        assert 'request.security(btcSym, "D", close[1], lookahead=barmerge.lookahead_on)' in text                             # completed daily close
        assert 'request.security(btcSym, "D", ta.sma(close, 50)[1], lookahead=barmerge.lookahead_on)' in text
        assert 'input.bool(%s, "Trend gate' % str(config["regime_gate"]).lower() in text
        assert 'input.bool(%s, "Gate the sell too' % str(config["rich_gate"]).lower() in text
        assert 'input.int(60, "Minutes between lag alerts"' in text and 'time - lastLagT >= coolMins * 60000' in text      # the cooldown
        assert 'alertcondition(%s, "Lag"' % ("lagBuy" if name == "mstx_projected.pine" else "buyBar") in text


def test_pine_average_is_in_daily_mstr_context_with_completed_offset():
    for name in monitor.PINE_FILES:
        text=(ROOT/name).read_text()
        assert 'ta.sma(dp, premiumN)[1]' in text
        # each session pairs with BTC's close of the same UTC day; lookahead off paired it with the day before (2026-10-01)
        assert 'db = request.security(btcSym, "D", close, lookahead=barmerge.lookahead_on)' in text
        assert 'ds = request.security(strcSym, "D", close, lookahead=barmerge.lookahead_on)' in text
        assert 'request.security(mstrSym, "D", priorPremium(), lookahead=barmerge.lookahead_on)' in text
        assert 'input.int(10, "Premium sessions"' in text
        assert '/ lineM - 1 - premiumAvg)' in text
        assert 'lineM * (1 + premiumAvg)' in text



def test_premium_survives_a_saved_state_reload(scenario,monkeypatch,premium_value):
    from io import BytesIO
    fetch=Mock(return_value=BytesIO(json.dumps({'premium':premium_value}).encode()))
    monkeypatch.setattr(monitor.urllib.request,'urlopen',fetch)
    assert monitor.main()==0
    fetch.assert_called_once()
    saved=json.loads(scenario.state.read_text())
    assert saved['premium_last']==premium_value
    fetch.reset_mock()
    fetch.side_effect=TimeoutError('offline')
    assert monitor.main()==0
    fetch.assert_called_once()
    restored=json.loads(scenario.state.read_text())
    assert restored['premium_last']==premium_value
    assert restored['proj_mstx']==saved['proj_mstx']
    assert monitor.PREMIUM_AVG==-.04


def test_loop_running_gate():
    runs = [{"databaseId": 100, "status": "in_progress"}, {"databaseId": 105, "status": "in_progress"}, {"databaseId": 90, "status": "completed"}]
    assert monitor.loop_running(runs) is True                     # the 5 minute run stands down for any live loop
    assert monitor.loop_running(runs, 105) is True                # the newer starter exits, the older one keeps the loop
    assert monitor.loop_running(runs, 100) is False               # the oldest in-progress run is the loop
    assert monitor.loop_running([{"databaseId": 90, "status": "completed"}]) is False
    assert monitor.loop_running("not a list") is False and monitor.loop_running([]) is False
    test = [{"databaseId": 90, "status": "in_progress", "displayTitle": "MSTR Session Loop (test)"}]
    assert monitor.loop_running(test) is False and monitor.loop_running(test, 100) is False   # a dry or forced run never holds the slot
    assert monitor.loop_running(test + [{"databaseId": 95, "status": "in_progress", "displayTitle": "MSTR Session Loop"}], 100) is True


def test_in_session_bounds():
    ny = monitor.NY
    assert not monitor.in_session(pd.Timestamp("2026-10-01 09:34", tz=ny).to_pydatetime())
    assert monitor.in_session(pd.Timestamp("2026-10-01 09:35", tz=ny).to_pydatetime())
    assert monitor.in_session(pd.Timestamp("2026-10-01 16:00:59", tz=ny).to_pydatetime())
    assert not monitor.in_session(pd.Timestamp("2026-10-01 16:01", tz=ny).to_pydatetime())
    assert not monitor.in_session(pd.Timestamp("2026-10-03 12:00", tz=ny).to_pydatetime())   # Saturday


class FakeClock:
    """Wall clock and monotonic clock that only move when the loop sleeps or a check takes time."""
    def __init__(self, start):
        self.t = 0.0; self.start = pd.Timestamp(start, tz=monitor.NY).to_pydatetime()
    def mono(self): return self.t
    def now(self): return self.start + pd.Timedelta(seconds=self.t).to_pytimedelta()
    def sleep(self, s): self.t += s


@pytest.mark.parametrize("check_s", [1, 20, 60])
def test_session_loop_cadence_commits_and_stops_at_close(scenario, monkeypatch, check_s):
    monkeypatch.setattr(monitor, "FORCE", False)
    clock, checks, commits = FakeClock("2026-10-01 15:50"), [], []
    def check():
        checks.append(clock.now()); clock.t += check_s            # a fast check exposed a 225 s commit gap live on 10/1
        if len(checks) == 3: monitor.PUSHES[0] += 1               # an alert on the third check
        return 0
    rc = monitor.session_loop(run_once=check, commit=lambda: commits.append(clock.now()), now_fn=clock.now,
                              clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330)
    assert rc == 0
    gaps = [(b - a).total_seconds() for a, b in zip(checks, checks[1:])]
    assert gaps and max(gaps) <= 120 and min(gaps) >= monitor.LOOP_PERIOD_S
    assert checks[-1].strftime("%H:%M") <= "16:00" and len(checks) >= 8
    assert commits[0] <= checks[0] + pd.Timedelta(seconds=check_s + 10).to_pytimedelta()          # the first check is committed at once
    assert any(checks[2] < c <= checks[2] + pd.Timedelta(seconds=check_s + 10).to_pytimedelta() for c in commits)   # and the alert at once
    cgaps = [(b - a).total_seconds() for a, b in zip(commits, commits[1:])]
    assert max(cgaps) <= 180                                                             # state reaches main at least every 3 minutes
    assert commits[-1] >= checks[-1]                                                     # the last check is committed on the way out


def test_session_loop_budget_and_off_hours(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "FORCE", False)
    clock = FakeClock("2026-10-01 10:00"); n = []
    monitor.session_loop(run_once=lambda: n.append(1) or 0, commit=lambda: None, now_fn=clock.now,
                         clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=10)
    assert 7 <= len(n) <= 9 and clock.t <= 11 * 60                                       # stops at the budget, well before 6 hours
    clock = FakeClock("2026-10-01 17:00"); n = []
    monitor.session_loop(run_once=lambda: n.append(1) or 0, commit=lambda: n.append("c"), now_fn=clock.now,
                         clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330)
    assert n == []                                                                       # after the close it does nothing at all
    clock = FakeClock("2026-10-01 09:10"); n = []
    monitor.session_loop(run_once=lambda: n.append(clock.now()) or 0, commit=lambda: None, now_fn=clock.now,
                         clock=clock.mono, sleep=clock.sleep, max_iter=2, budget_min=330)
    assert n[0].strftime("%H:%M") == "09:35"                                             # a starter just before the open waits for it


def test_session_loop_survives_a_crashing_check(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "FORCE", True)
    clock, n = FakeClock("2026-10-01 18:00"), []
    def check():
        n.append(1)
        if len(n) == 1: raise RuntimeError("boom")
        return 0
    rc = monitor.session_loop(run_once=check, commit=lambda: None, now_fn=clock.now, clock=clock.mono, sleep=clock.sleep, max_iter=3, budget_min=330)
    assert len(n) == 3 and rc == 0


def test_session_loop_budget_runs_from_process_start(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "FORCE", False)
    clock, n = FakeClock("2026-10-01 09:00"), []          # the 13:00 UTC starter in summer waits 35 minutes for the open
    monitor.session_loop(run_once=lambda: n.append(clock.now()) or 0, commit=lambda: None, now_fn=clock.now,
                         clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330)
    assert n[0].strftime("%H:%M") == "09:35" and clock.t <= 330 * 60 + 75             # ends inside timeout-minutes 350, not at 15:05
    clock, n = FakeClock("2026-10-01 10:35"), []          # a loop started after about 10:30 runs to the close
    monitor.session_loop(run_once=lambda: n.append(clock.now()) or 0, commit=lambda: None, now_fn=clock.now,
                         clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330)
    assert n[-1].strftime("%H:%M") in ("15:59", "16:00")


def test_session_loop_settles_hands_over_and_keeps_a_failed_push_dirty(scenario, monkeypatch):
    monkeypatch.setattr(monitor, "FORCE", False)
    def run(pushed):
        clock, events, peers = FakeClock("2026-10-01 11:00"), [], iter([False, False, True])
        monitor.session_loop(run_once=lambda: events.append("check") or 0, commit=lambda: events.append("commit") or (None if pushed else False),
                             now_fn=clock.now, clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330,
                             older_loop=lambda: next(peers), before_first=lambda: events.append("settle"))
        return events
    # settle once before the first check; the third peer check finds an older loop, so the loop hands over after two checks,
    # committing the second check on the way out
    assert run(True) == ["settle", "check", "commit", "check", "commit"]
    clock, events, peers = FakeClock("2026-10-01 11:00"), [], iter([False, True])
    monitor.session_loop(run_once=lambda: events.append("check") or 0, commit=lambda: events.append("commit") or False,
                         now_fn=clock.now, clock=clock.mono, sleep=clock.sleep, max_iter=0, budget_min=330,
                         older_loop=lambda: next(peers), before_first=lambda: None)
    assert events == ["check", "commit", "commit"]        # the first check's push failed, so it stays dirty and is retried on the way out


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("git")), reason="needs bash and git")
def test_commit_script_moves_only_the_pine_default_edits_and_fails_loudly():
    """sync_pine's edit goes over as a patch, so an indicator code push that lands mid-run survives (10/1, 1d38e010)."""
    bash = shutil.which("bash")             # a full path: on Windows a bare "bash" can resolve to WSL before PATH
    script = (ROOT / ".github" / "workflows" / "commit_state.sh").read_text()
    with tempfile.TemporaryDirectory(prefix=".monitor-test-", dir=ROOT, ignore_cleanup_errors=True) as folder:
        top = Path(folder); origin, run, other = top / "origin.git", top / "run", top / "other"
        def git(*args, cwd=top): return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout
        git("init", "-q", "--bare", "-b", "main", str(origin))
        pine = 'input.float(100, "BTC held")\n// body v1\nplot(close)\n'
        for clone in (other, run):
            git("clone", "-q", str(origin), str(clone))
            for k, v in (("core.autocrlf", "false"), ("user.name", "t"), ("user.email", "t@t")): git("config", k, v, cwd=clone)
            if clone == other:
                (other / "mstr_projected.pine").write_text(pine, newline="\n")
                (other / "mstr_state.json").write_text('{"last_run": "old"}\n', newline="\n")
                git("add", ".", cwd=other); git("commit", "-q", "-m", "seed", cwd=other); git("push", "-q", "origin", "HEAD:main", cwd=other)
        # the run: sync_pine moves an input default and the check writes state; meanwhile an indicator code push lands on main
        (run / "mstr_projected.pine").write_text(pine.replace("100", "200"), newline="\n")
        (run / "mstr_state.json").write_text('{"last_run": "new"}\n', newline="\n")
        (other / "mstr_projected.pine").write_text("// header\n" + pine.replace("body v1", "body v2"), newline="\n")   # next line and an offset
        git("commit", "-q", "-am", "code", cwd=other); git("push", "-q", "origin", "HEAD:main", cwd=other)
        r = subprocess.run([bash, "-c", script], cwd=run, capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        main_pine = git("show", "main:mstr_projected.pine", cwd=origin)
        assert "body v2" in main_pine and 'input.float(200, "BTC held")' in main_pine     # both changes reach main
        assert '"new"' in git("show", "main:mstr_state.json", cwd=origin)
        (run / "mstr_state.json").write_text('{"last_run": "newer"}\n', newline="\n")
        git("remote", "set-url", "origin", str(top / "missing.git"), cwd=run)
        r = subprocess.run([bash, "-c", script], cwd=run, capture_output=True, text=True)
        assert r.returncode == 1 and "not pushed after 3 tries" in r.stdout                 # a push that never lands is not reported as success


def test_sheet_fit_from_the_site_config():
    """The site's fit became Alex's sheet on 10/5 (kind 'sheet', no a/c/par); the monitor crashed on fit['a']."""
    import mstr_gap as monitor
    try:
        monitor.configure({"fit": {"kind": "sheet", "b": 0.025}})
        assert monitor.target(99.58, 75000) == pytest.approx(0.900)
        assert monitor.target(99.58, 85000) == pytest.approx(1.000)
        assert monitor.target(99.58, 137500) == pytest.approx(1.525)
        assert monitor.target(96, 75000) == pytest.approx(0.885)
        assert monitor.target(90, 75000) == pytest.approx(0.8375)
        assert monitor.target(75, 75000) == pytest.approx(0.775)
        assert monitor.target(100, 200000) == 2
        assert monitor.target(98.7, 100000, monitor.LAG_SLOPE) == pytest.approx(1.025)   # the lag keeps its own slope
    finally:
        monitor.configure({})


def test_yahoo_daily_glitch_waits_one_run_before_paging(scenario, monkeypatch):
    """10/5 9:24 AM and 10/6 2:43 PM: Yahoo's daily BTC reply came back without timestamps and paged 'Monitor error'."""
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    calls = []
    def ticker(t):
        calls.append(t)
        return SimpleNamespace(history=lambda **kwargs: pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.Index([0, 1])))
    monkeypatch.setattr(monitor.yf, "Ticker", ticker)
    assert monitor.main() == 1
    assert calls.count("BTC-USD") == 3                                   # retried before giving up
    assert not [t for t, _, _ in scenario.sent if t == "Monitor error"]  # one bad run does not page
    assert json.loads(scenario.state.read_text())["glitch_runs"] == 1
    assert monitor.main() == 1
    pages = [m for t, m, _ in scenario.sent if t == "Monitor error"]
    assert len(pages) == 1 and "without timestamps" in pages[0]          # the second bad run in a row pages


def test_glitch_counter_resets_on_a_clean_run(scenario, monkeypatch):
    state = json.loads(scenario.state.read_text()); state["glitch_runs"] = 1; scenario.state.write_text(json.dumps(state))
    monitor.main()
    assert json.loads(scenario.state.read_text())["glitch_runs"] == 0
