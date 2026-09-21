"""Offline reliability checks; all temporary files stay inside this clone."""
import ast
import json
from pathlib import Path
import re
import socket
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
    config = json.loads((ROOT / "mstr_config.json").read_text())
    monitor.configure(config)
    paths = []
    for name in monitor.PINE_FILES:
        original = (ROOT / name).read_bytes()
        path = tmp_path / name
        path.write_bytes(original)
        paths.append(str(path))
    monkeypatch.setattr(monitor, "PINE_FILES", paths)
    assert monitor.sync_pine(900000, 500)
    for path in paths:
        raw = Path(path).read_bytes()
        assert raw.count(b"\n") == raw.count(b"\r\n")
        text = raw.decode()
        assert 'input.float(%s, "Fitted intercept"' % repr(config["fit"]["a"]) in text
        assert 'input.float(%s, "Target mNAV slope' % repr(config["fit"]["b"]) in text
        assert 'target = math.min(2.0, fitA + fitC * math.max(0, fitPar - strc)' in text
        if Path(path).name == "mstx_projected.pine":
            assert 'input.float(0.0125, "Lag slope' in text
            assert 'input.float(%g, "Cheap line, MSTX' % (200*config['cheap_threshold']) in text
            assert 'input.float(%g, "Rich line, MSTX' % (200*config['rich_threshold']) in text
        else:
            assert 'input.float(0.025, "Legacy lag slope' in text
            assert 'input.float(%g, "Cheap line' % (100*config['cheap_threshold']) in text
            assert 'input.float(%g, "Rich line' % (100*config['rich_threshold']) in text
        assert 'lagBase(strc) + lagSlope * (btc - 75000) / 2500' in text
    assert not monitor.sync_pine(900000, 500)


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


def test_pine_average_is_in_daily_mstr_context_with_completed_offset():
    for name in monitor.PINE_FILES:
        text=(ROOT/name).read_text()
        assert 'ta.sma(dp, premiumN)[1]' in text
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
