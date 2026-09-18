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
        mstx.iloc[-1] = 120.0
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
