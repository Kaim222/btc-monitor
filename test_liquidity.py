import json

import liquidity


def row(stamp, usd):
    return [str(stamp), "0", "0", str(usd)]


def candle(stamp, high=101, low=99, close=100):
    return [str(stamp), "0", str(high), str(low), str(close)]


def test_buy_increase_creates_only_longs_at_expected_price():
    result = liquidity.liquidation_model(
        [row(1, 1_000_000), row(2, 2_000_000)], [["2", "0", "10"]],
        [candle(1, 10100, 9900, 10000), candle(2, 10100, 9900, 10000)], 9800)
    expected = 10000 * (1 - .1 + .005)
    assert int(expected // liquidity.BUCKET) * liquidity.BUCKET == 9000
    assert any(level[0] == 9000 and level[1] == 300_000 for level in result["levels"])
    assert all(level[2] == 0 for level in result["levels"])


def test_crossed_long_is_removed_but_short_survives():
    oi = [row(1, 1_000_000), row(2, 2_000_000), row(3, 2_000_000)]
    taker = [["2", "1", "1"]]
    candles = [candle(1, 101, 99), candle(2, 101, 99), candle(3, 102, 89)]
    result = liquidity.liquidation_model(oi, taker, candles, 100, bucket=1)
    assert all(level[1] == 0 for level in result["levels"])
    assert any(level[2] > 0 for level in result["levels"])


def test_oi_decrease_scales_positions_and_empty_decrease_is_safe():
    base = liquidity.liquidation_model(
        [row(1, 1_000_000), row(2, 2_000_000)], [["2", "0", "1"]],
        [candle(1), candle(2)], 100, bucket=1)
    scaled = liquidity.liquidation_model(
        [row(1, 1_000_000), row(2, 2_000_000), row(3, 1_500_000)], [["2", "0", "1"]],
        [candle(1), candle(2), candle(3, 100, 100)], 100, bucket=1)
    assert sum(level[1] for level in scaled["levels"]) == sum(level[1] for level in base["levels"]) // 2
    assert liquidity.liquidation_model(
        [row(1, 2_000_000), row(2, 1_000_000)], [], [candle(1), candle(2)], 100)["levels"] == []


def test_output_shape_filtering_and_types():
    result = liquidity.liquidation_model(
        [row(1, 0), row(2, 1_000_000)], [["2", "1", "1"]],
        [candle(1), candle(2)], 100, bucket=1)
    assert set(result) == {"levels", "near", "window_h", "okx_oi_usd"}
    assert result["levels"] == sorted(result["levels"])
    assert all(type(value) is int for level in result["levels"] for value in level)
    assert all((long == 0 or lower + .5 < 100) and (short == 0 or lower + .5 > 100)
               and abs((lower + .5) / 100 - 1) <= .08 and max(long, short) >= 50_000
               for lower, long, short in result["levels"])
    assert all(type(value) is int for value in result["near"].values())


def test_book_depth_and_nearby_walls():
    book = {"bids": [["99", "2", "1"], ["96", "3", "1"], ["1", "999", "1"]],
            "asks": [["101", "4", "1"], ["104", "5", "1"], ["200", "999", "1"]]}
    result = liquidity.book_summary(book, 100)
    assert result["bid_1"] == 2.0 and result["bid_5"] == 5.0
    assert result["ask_1"] == 4.0 and result["ask_5"] == 9.0
    assert [1.0, 999.0] not in result["bid_walls"]
    assert [200.0, 999.0] not in result["ask_walls"]


def test_network_failure_preserves_existing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = b'{"old": true}\n'
    (tmp_path / liquidity.OUTPUT).write_bytes(original)

    def fail(_url):
        raise OSError("offline")

    assert liquidity.main(fetch=fail) == 0
    assert (tmp_path / liquidity.OUTPUT).read_bytes() == original


def liquidation(stamp, side, size, price):
    return {"ts": str(stamp), "posSide": side, "sz": str(size), "bkPx": str(price)}


def test_recent_summary_windows_hourly_biggest_and_coverage():
    now = 24 * 3_600_000 + 30 * 60_000
    events = [
        liquidation(now - 10 * 60_000, "long", 2, 100),
        liquidation(now - 45 * 60_000, "short", 3, 200),
        liquidation(now - 3 * 3_600_000, "long", 10, 300),
        liquidation(now - 23 * 3_600_000, "short", 20, 400),
    ]
    result = liquidity.recent_summary(events, now, .01)
    assert result["sums"] == {"m30": [2, 0], "h1": [2, 6], "h4": [32, 6],
                              "h12": [32, 6], "h24": [32, 86]}
    assert len(result["hourly"]) == 24
    assert result["hourly"][-1] == ["1970-01-01T23:30:00Z", 2, 6]   # rolling hour ending now
    assert result["hourly"][0][1:] == [0, 80]   # the 23h-old short sits in the oldest hour
    assert any(item[1:] == [0, 0] for item in result["hourly"])
    assert result["biggest"][0][1:] == ["short", 80, 400.0]
    assert result["covered_h"] == 23.0


def test_fetch_recent_pages_and_deduplicates():
    now = 30 * 3_600_000
    duplicate = liquidation(now - 60_000, "long", 10, 100)
    older = liquidation(now - 25 * 3_600_000, "short", 5, 100)
    urls = []

    def fetch(url):
        urls.append(url)
        if url == liquidity.INSTRUMENT_URL:
            return {"code": "0", "data": [{"ctVal": "0.01"}]}
        details = [duplicate] if "after=" not in url else [duplicate, older]
        return {"code": "0", "data": [{"details": details}]}

    result = liquidity.fetch_recent(fetch, now, pause=lambda _seconds: None)
    assert result["sums"]["h24"] == [10, 0]
    assert len(urls) == 3
    assert "after=" in urls[-1]


def test_liquidation_failure_still_writes_report_without_recent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payloads = {
        liquidity.OI_URL: {"code": "0", "data": [row(1, 1_000_000)]},
        liquidity.TAKER_URL: {"code": "0", "data": [["1", "1", "1"]]},
        liquidity.CANDLE_URL: {"code": "0", "data": [candle(1)]},
        liquidity.PRICE_URL: {"price": "100"},
        liquidity.BOOK_URL: {"bids": [], "asks": []},
    }

    def fetch(url):
        if url == liquidity.INSTRUMENT_URL:
            raise OSError("liquidations offline")
        return payloads[url]

    assert liquidity.main(fetch=fetch) == 0
    report = json.loads((tmp_path / liquidity.OUTPUT).read_text())
    assert "recent" not in report
    assert report["price"] == 100


def test_hourly_strip_covers_the_same_24h_as_h24():
    now = 24 * 3_600_000 + 30 * 60_000   # 00:30 the next day: the oldest half hour used to fall outside the strip
    result = liquidity.recent_summary([liquidation(now - (23 * 60 + 45) * 60_000, "long", 1200, 83680.6)], now, .01)
    assert result["sums"]["h24"] == [1004167, 0]
    assert result["hourly"][0][1:] == [1004167, 0]
    assert sum(item[1] for item in result["hourly"]) == 1004167


def test_blank_error_message_still_writes_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    payloads = {
        liquidity.OI_URL: {"code": "0", "data": [row(1, 1_000_000)]},
        liquidity.TAKER_URL: {"code": "0", "data": [["1", "1", "1"]]},
        liquidity.CANDLE_URL: {"code": "0", "data": [candle(1)]},
        liquidity.PRICE_URL: {"price": "100"},
        liquidity.BOOK_URL: {"bids": [], "asks": []},
    }

    def fetch(url):
        if url == liquidity.INSTRUMENT_URL:
            raise TimeoutError()
        return payloads[url]

    assert liquidity.main(fetch=fetch) == 0
    report = json.loads((tmp_path / liquidity.OUTPUT).read_text())
    assert "recent" not in report and report["price"] == 100
