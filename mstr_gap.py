"""
MSTR gap monitor: fires Pushover when MSTR trades away from Alex's projected price.

Projected MSTR = BTC x (BTC held / shares) x target mNAV, target = STRC rule + slope x (BTC - 75,000) / 2,500.
gap = MSTR / projected - 1.

Three alerts:
  LAG    the gap falls past lag_threshold (config, -1.5% MSTR = -3.0% MSTX) below its own average over the previous
         hour, while BTC has held (BTC's own hour move better than -1%), measured at MSTR's bar LOW, which is where
         mstx_projected.pine measures it. The lag uses its OWN slope (config lag_slope 0.0125), not the projected
         price's 0.025: 0.0125 at a -3.0% MSTX line was the best hit rate tested, 17 events in 60 days, 65% positive.
         Alex's call, 9/17. Every bar since the previous run is scanned and the deepest one is judged, so
         a lag that lives in one minute is not missed by a five-minute poll. Cooldown 60 minutes. Readings past
         lag_watch (-1.0% MSTR = -2.0% MSTX) are written to the ledger without a push, so near misses are on the record.
         These three settings were wrong until 2026-09-17: the threshold was -1.5% MSTR, the measure was the close, and
         only the newest bar was judged. Alex took a lag trade at 11:13 that day and no push ever went out.
  CHEAP  MSTR is under the cheap line vs projection (config, -3% MSTR = -6% MSTX). Fires on the cross, again on each full
         point further, and hourly while it holds. Carries BTC's 50-day state as context (not a gate).
  RICH   MSTR is over the rich line (config, +4% MSTR = +8% MSTX). Same cadence. NOT gated (rich_gate false since 9/17):
         it declined with BTC both above and below its 50-day. The push carries STRC's price and discount to par as plain
         context; the read that a cheap STRC sharpens Rich was tested 9/17 and reversed under a non-circular framing.
  BAND   the ladder band on the monthly close (Kaim power-law quantile: under 10 MSTX, 10 to 60 MSTR, 60 to 75 IBIT, 75 and up
         the sell zone), pushed when a month's close moves it. The 50-day crossing, Cheap and Rich pushes carry the ladder plays
         (the site's data/ladder-rules.json is the written version).
  Every alert leads with MSTX vs projected MSTX (yesterday's close moved 2x MSTR's projected move), then MSTR.
  Regime gate (config regime_gate, default on): Lag and Cheap push only with BTC above its 50-day, which is the rule
  Alex stated for the BUY signals. Rich is NOT gated (rich_gate false): it declined in both regimes, and the
  mirror-image rule was a symmetry assumption, never his. Muted alerts are still logged and scored.

Holdings and the assumed diluted share count come from api.strategy.com/btc/bitcoinKpis on every run (btcHoldings and
satsPerShare; this reproduces strategy.com/shares' ADSO exactly), so Monday's 8-K flows through by itself. Thresholds and the
slope come from the ladder site's data/mstr-config.json (fetched live from GitHub; editing that file changes both the site and
this monitor); set btc_held or shares_m there only to override the API. A local mstr_config.json is the fallback. State in mstr_state.json. Every alert is scored on later
runs (MSTR minus BTC, and MSTX itself, over the next 30 and 60 minutes) into mstr_ledger.json, so the rule keeps a record of itself.
Regular session only (9:35 to 16:00 New York). Env: PUSHOVER_TOKEN, PUSHOVER_USER; without them it prints instead of
sending. Flags: --force (run outside market hours on the last session's bars), --test (send one test message).
"""
import os, sys, json, math, time, traceback, urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pandas as pd, yfinance as yf

NY = ZoneInfo("America/New_York")
CONFIG_URL = "https://raw.githubusercontent.com/Kaim222/btc-quantile-ladder/main/data/mstr-config.json"
CONFIG_FILE, STATE_FILE, LEDGER_FILE = "mstr_config.json", "mstr_state.json", "mstr_ledger.json"
FORCE, TEST = "--force" in sys.argv, "--test" in sys.argv

def load(path, default):
    if os.path.exists(path):
        with open(path) as f: return json.load(f)
    return default
def load_config():
    try:
        req = urllib.request.Request(CONFIG_URL + "?t=%d" % int(datetime.now().timestamp()), headers={"User-Agent": "mstr-gap-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r: cfg = json.loads(r.read())
        if not isinstance(cfg.get("fit"), dict): raise ValueError("remote config has no fitted line")
        cfg["_source"] = "ladder site"
        return cfg
    except Exception as e:
        print("config from the ladder site failed (%s); using the local file" % e)
        cfg = load(CONFIG_FILE, {}); cfg["_source"] = "local"; return cfg
FIT_DEFAULT = {'a': 0.9349260602738632, 'b': 0.030355691245129924, 'c': -0.0029161736798696616, 'par': 100}
cfg = {}
STRATEGY_API = "https://api.strategy.com/btc/bitcoinKpis"
def strategy_holdings(state):
    """Return live (btc_held, shares_m, source); main keeps cached/config values on failure."""
    try:
        req = urllib.request.Request(STRATEGY_API, headers={"User-Agent": "Mozilla/5.0 mstr-gap-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r: k = json.loads(r.read())["results"]
        held = float(str(k["btcHoldings"]).replace(",", "")); sps = float(k["satsPerShare"])
        shares_m = held / (sps / 1e8) / 1e6
        if held > 100000 and 100 < shares_m < 5000:
            state["strategy_last"] = {"btc_held": held, "shares_m": round(shares_m, 3), "as_of": k.get("msTimestamp"), "fetched": datetime.now(NY).isoformat()}
            return held, shares_m, "strategy.com live"
    except Exception as e:
        raise RuntimeError("strategy.com holdings failed: %s" % e) from e
    raise ValueError("strategy.com returned invalid holdings")
PINE_FILES = ["mstr_gap_lag.pine", "mstr_projected.pine", "mstx_projected.pine"]
def sync_pine(held, shares_m):
    """Rewrite the indicators' default inputs from the live holdings and config, so a re-paste carries the real numbers.

    Price coefficients and gap thresholds follow the fitted config. Lag settings stay independent.
    """
    import re
    subs = [(r'input\.float\([0-9.]+, "BTC held"', 'input.float(%d, "BTC held"' % int(round(held))),
            (r'input\.float\([0-9.]+, "Assumed diluted shares \(M\)"', 'input.float(%.3f, "Assumed diluted shares (M)"' % shares_m),
            (r'input\.float\([0-9.]+, "Target mNAV slope per \$2,500 of BTC"', 'input.float(%s, "Target mNAV slope per $2,500 of BTC"' % (repr(SLOPE))),
            (r'input\.float\([0-9.]+, "Lag slope per \$2,500 of BTC \(the lag runs on its own\)"', 'input.float(%s, "Lag slope per $2,500 of BTC (the lag runs on its own)"' % ("%g" % LAG_SLOPE)),
            (r'input\.float\(-?[0-9.]+, "Lag alert, MSTX % vs the trailing window"', 'input.float(%g, "Lag alert, MSTX %% vs the trailing window"' % (100 * LAG_X)),
            (r'input\.float\(-?[0-9.]+, "Cheap line, MSTX % under projection"', 'input.float(%g, "Cheap line, MSTX %% under projection"' % (100 * CHEAP_X)),
            (r'input\.float\(-?[0-9.]+, "Rich line, MSTX % over projection"', 'input.float(%g, "Rich line, MSTX %% over projection"' % (100 * RICH_X))]
    for key, label in [("a", "Fitted intercept"), ("c", "Fitted STRC shortfall"), ("par", "STRC par")]:
        subs.append((r'input\.float\(-?[0-9.]+, "' + re.escape(label) + '"', 'input.float(%s, "%s"' % (repr(FIT[key]), label)))
    for label, value in [("Cheap line (% under projection)", CHEAP * 100), ("Rich line (% over projection)", RICH * 100)]:
        subs.append((r'input\.float\(-?[0-9.]+, "' + re.escape(label) + '"', 'input.float(%g, "%s"' % (value, label)))
    changed = False
    for pf in PINE_FILES:
        if not os.path.exists(pf): continue
        raw = open(pf, "rb").read()
        newline = "\r\n" if b"\r\n" in raw else "\n"
        src = new = raw.decode("utf-8").replace("\r\n", "\n")
        for pat, rep in subs: new = re.sub(pat, rep, new)
        if new != src:
            with open(pf, "wb") as out: out.write(new.replace("\n", newline).encode("utf-8"))
            changed = True
    return changed
def configure(config, held=None, shares=None, source="none"):
    global cfg, HOLD_SRC, BTC_HELD, SHARES_M, SLOPE, LAG, CHEAP, RICH, LAG_X, CHEAP_X, RICH_X
    global BTC_HOLD, GATE, RICH_GATE, LAG_SLOPE, LAG_AT_LOW, LAG_WATCH, BPS, FIT
    cfg = config
    _h, _s, HOLD_SRC = held, shares, source
    if cfg.get("btc_held") not in (None, "", "auto"): _h, HOLD_SRC = float(cfg["btc_held"]), "config override"
    if cfg.get("shares_m") not in (None, "", "auto"): _s = float(cfg["shares_m"]); HOLD_SRC = "config override"
    BTC_HELD = _h if _h else 845050.0; SHARES_M = _s if _s else 450.112
    FIT = dict(cfg.get("fit", FIT_DEFAULT))
    if not all(math.isfinite(float(FIT[k])) for k in ("a", "b", "c", "par")) or FIT["c"] > 0:
        raise ValueError("Invalid fitted line")
    SLOPE = float(FIT["b"])
    LAG, CHEAP, RICH = float(cfg.get("lag_threshold", -0.015)), float(cfg.get("cheap_threshold", -0.085)), float(cfg.get("rich_threshold", 0.10))
    centre = 0.0
    LAG_X, CHEAP_X, RICH_X = 2 * LAG, 2 * (centre + CHEAP), 2 * (centre + RICH)   # MSTX terms: the indicator draws all three lines on the MSTX gap, so the tests run there too
    BTC_HOLD = float(cfg.get("btc_hour_move_floor", -0.01))
    GATE = bool(cfg.get("regime_gate", True))          # LAG and CHEAP only: the buy signals want BTC above its 50-day
    RICH_GATE = bool(cfg.get("rich_gate", False))      # Rich fires in either regime; it declined in both on the daily data
    LAG_SLOPE = float(cfg.get("lag_slope", 0.0125))         # the lag has its own slope; the projected price uses SLOPE
    LAG_AT_LOW = bool(cfg.get("lag_at_low", True))          # measure the lag at MSTR's bar low, where the indicator measures it
    LAG_WATCH = float(cfg.get("lag_watch", -0.01))        # log a row at this depth even when nothing fires, so near misses are on the record
    BPS = BTC_HELD / (SHARES_M * 1e6)

configure({})

def lag_base(s):
    if s >= 97.5: return 0.90
    if s >= 95: return 0.875 + (s - 95) * 0.01
    if s >= 92.5: return 0.85 + (s - 92.5) * 0.01
    if s >= 87.5: return 0.825 + (s - 87.5) * 0.005
    if s >= 82.5: return 0.80 + (s - 82.5) * 0.005
    return max(0.775, 0.775 + (s - 77.5) * 0.005)
def target(strc, btc, slope=None):
    # Preserve the independent lag calculation exactly, including its historical base.
    if slope is not None:
        return lag_base(strc) + slope * (btc - 75000) / 2500
    return min(2.0, FIT["a"] + FIT["c"] * max(0, FIT["par"] - strc) + FIT["b"] * (btc - 75000) / 2500)

# The ladder: Kaim power law model v2, the same constants as the site (its data/ladder-model.json, fitted 2026-09-19, refit yearly).
# Centre line 5.645315 x log10(days since 2009-01-03) - 16.430264 (least squares slope, intercept at the median of the gaps). Lines are
# log10 offsets c x exp(-age / T), age in years since the genesis block: 15, 85 and 95 are nominal shares of all days under the line,
# Floor (0.1) and Ceiling (99.9) are envelopes of every close since 2014. Model v1 was A 5.82, B -17.029 with straight-line decaying upper bands.
_CLOCK = datetime(2009, 1, 3, tzinfo=timezone.utc)
_MODEL_A, _MODEL_B = 5.645315, -16.430264
_BANDS = [(99.9, 2.468226, 8.871781), (95, 1.589109, 8.871781), (85, 1.048162, 8.871781), (50, 0.0, None), (15, -0.339276, 20.784638), (0.1, -0.659285, 20.784638)]
def _days(ts):
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
    return (ts - _CLOCK).total_seconds() / 86400
def _band_offsets(ts):
    age = _days(ts) / 365.25
    return [(q, c if T is None else c * math.exp(-age / T)) for q, c, T in _BANDS]
def fair_value(ts): return 10 ** (_MODEL_A * math.log10(_days(ts)) + _MODEL_B)
def ladder_q(price, ts):
    res, bs = math.log10(price / fair_value(ts)), _band_offsets(ts)
    for (hq, ho), (lq, lo) in zip(bs, bs[1:]):
        if lo <= res <= ho: return lq + (res - lo) / (ho - lo) * (hq - lq)
    if res > bs[0][1]: return min(99.99, bs[0][0] + (res - bs[0][1]) * (bs[0][0] - bs[1][0]) / (bs[0][1] - bs[1][1]))
    return max(0.01, bs[-1][0] + (res - bs[-1][1]) * (bs[-2][0] - bs[-1][0]) / (bs[-2][1] - bs[-1][1]))
def ladder_price(q, ts):
    bs = _band_offsets(ts)
    for (hq, ho), (lq, lo) in zip(bs, bs[1:]):
        if lq <= q <= hq: return fair_value(ts) * 10 ** (lo + (q - lq) / (hq - lq) * (ho - lo))
    (q0, o0), (q1, o1) = (bs[0], bs[1]) if q > bs[0][0] else (bs[-1], bs[-2])        # past either end, the outer pair's slope, as the site does
    return fair_value(ts) * 10 ** (o0 + (o0 - o1) / (q0 - q1) * (q - q0))
LADDER_LINES = (10, 60, 75)          # moved from 15 / 50 / 85 on 2026-09-20 after the cut-point study on real prices since 2020
LINES_TXT = " / ".join(str(x) for x in LADDER_LINES)
def ladder_band(q): return "MSTX" if q < LADDER_LINES[0] else "MSTR" if q < LADDER_LINES[1] else "IBIT" if q < LADDER_LINES[2] else "sell zone"
BAND_LINE = {"MSTX": LADDER_LINES[0], "MSTR": LADDER_LINES[1], "IBIT": LADDER_LINES[2]}          # the band ceiling, where the short goes and the rotation triggers
BAND_PLAY = {"MSTX": "MSTX PMCC: long 12 months at 0.75 delta, short 90 days at the %d line, rolled." % LADDER_LINES[0],
             "MSTR": "MSTR PMCC: long 12 months at 0.75 delta, short 90 days at the %d line, rolled." % LADDER_LINES[1],
             "IBIT": "IBIT PMCC: long 12 months at 0.75 delta, short 90 days at the %d line, rolled. Rich readings here are the sell." % LADDER_LINES[2],
             "sell zone": "Sell the BTC beta into it and rotate down; the proceeds sit in STRC."}

def send_pushover(title, message, sound="cashregister", priority=0):
    for attempt in range(3):
        try:
            token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
            if not token or not user:
                print("[dry run, no Pushover keys]\n" + title + "\n" + message.replace("<b>", "").replace("</b>", ""))
                return True
            data = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": message,
                                           "html": "1", "sound": sound, "priority": priority}).encode()
            req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.loads(r.read())
            if result.get("status") != 1:
                raise RuntimeError("Pushover error: %s" % result)
            print("Pushover sent: " + title)
            return True
        except Exception:
            traceback.print_exc()
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return False


def bars(ticker, interval="1m", period="2d", field="Close"):   # BTC "1d" is the UTC day and goes empty after 8 PM New York, so two days
    h = yf.Ticker(ticker).history(period=period, interval=interval, prepost=False)
    if h.empty: raise RuntimeError("no %s bars for %s" % (interval, ticker))
    h.index = h.index.tz_convert(NY)
    return h[field] if isinstance(field, str) else h[list(field)]

def score_ledger(df, ledger):
    """Fill in the 30 and 60 minute outcomes (MSTR minus BTC, in percent) for alerts that now have the bars to score them."""
    changed = False
    for e in ledger:
        if e.get("scored"): continue
        t0 = datetime.fromisoformat(e["time"]); day = df[df.index.date == t0.date()]
        if day.empty: continue
        after = day[day.index > t0]
        if len(after) < 60 and not (len(after) and after.index[-1].time() >= datetime.strptime("15:59", "%H:%M").time()): continue
        base_m, base_b = e["mstr"], e["btc"]
        for h in (30, 60):
            k = min(h, len(after)) - 1
            if k < 0: continue
            e["mstr_%dm" % h] = round(100 * (float(after["MSTR"].iloc[k]) / base_m - 1), 2)
            e["btc_%dm" % h] = round(100 * (float(after["BTC"].iloc[k]) / base_b - 1), 2)
            e["rel_%dm" % h] = round(e["mstr_%dm" % h] - e["btc_%dm" % h], 2)
            if e.get("mstx") and "MSTX" in after.columns:
                e["mstx_%dm" % h] = round(100 * (float(after["MSTX"].iloc[k]) / float(e["mstx"]) - 1), 2)   # what MSTX itself did
        e["scored"] = True; changed = True
    return changed

def main():
    now = datetime.now(NY)
    state, ledger = {}, []
    errors, error_messages = [], []

    def failed(name, exc):
        traceback.print_exc()
        errors.append(name)
        error_messages.append("%s: %s" % (name, exc))

    def push(*args, **kwargs):
        if not send_pushover(*args, **kwargs):
            raise RuntimeError("Pushover delivery failed after three attempts: %s" % args[0])

    def run():
        cached = state.get("strategy_last") or {}
        configure(load_config(), cached.get("btc_held"), cached.get("shares_m"),
                  "strategy.com cached %s" % cached.get("fetched", "")[:16] if cached else "none")
        try:
            _prev = dict(cached)
            _h, _s, source = strategy_holdings(state)
            configure(cfg, _h, _s, source)
            if _h and _s and "override" not in HOLD_SRC:
                sync_pine(_h, _s)
                moved = _prev and (abs(float(_prev.get("btc_held", 0)) - _h) >= 1 or abs(float(_prev.get("shares_m", 0)) - _s) >= 0.001)
                if moved:
                    push("Holdings changed", "Strategy now shows <b>%s BTC</b> over <b>%.3fM</b> assumed diluted shares (was %s / %.3fM). The site and this monitor already use the new numbers. Type the two numbers into the TradingView indicator's settings (or re-paste mstx_projected.pine from the monitor repo, its defaults are updated)." % (
                        format(int(_h), ","), _s, format(int(float(_prev.get("btc_held", 0))), ","), float(_prev.get("shares_m", 0))), sound="magic", priority=-1)
        except Exception as exc:
            failed("holdings", exc)
        if TEST:
            push("Test", "Wired. Holdings %s (%s): BTC held %s, shares %.3fM. Thresholds from the %s: slope %.4f per $2,500, lag %.1f%% inside an hour, cheap %.0f%%, rich +%.0f%%." % (
                HOLD_SRC, "auto" if "override" not in HOLD_SRC else "manual", format(int(BTC_HELD), ","), SHARES_M, cfg["_source"], SLOPE, 100 * LAG, 100 * CHEAP, 100 * RICH), sound="siren", priority=1)
            return
        # BTC's daily close vs its 50-day, and the ladder band on the monthly close: checked on every run, in or out of the session, pushed on a change.
        # Completed UTC days only (the rule is the daily close, so the crossing fires once, on the first run after the close, never on a wick).
        btc_daily = yf.Ticker("BTC-USD").history(period="130d")["Close"].dropna()
        utc_now = datetime.now(timezone.utc); tz = btc_daily.index.tz
        done = btc_daily[btc_daily.index < pd.Timestamp(utc_now.year, utc_now.month, utc_now.day, tz=tz)]
        btc50 = float(done.tail(50).mean()); btc_close = float(done.iloc[-1])
        regime_now = "above" if btc_close > btc50 else "below"
        try:
            # the band: the prior month's last daily close, run through the ladder at the month-end instant; a touch on the daily is not a rotation
            m0 = pd.Timestamp(utc_now.year, utc_now.month, 1, tz=tz); mclose = btc_daily[btc_daily.index < m0]
            if len(mclose) and (m0 - mclose.index[-1]) <= pd.Timedelta(days=1):
                m_end, m_px = (m0 - pd.Timedelta(milliseconds=1)).to_pydatetime(), float(mclose.iloc[-1])
                q_m = ladder_q(m_px, m_end); band_now = ladder_band(q_m); prev_band = state.get("band")
                if prev_band and state.get("band_lines") != LINES_TXT and state.get("band_close") == m_end.strftime("%Y-%m-%d"):
                    push("Ladder lines: %s" % LINES_TXT, "The ladder lines moved to <b>%s</b>: MSTX under %d, MSTR %d to %d, IBIT %d to %d, the sell zone %d and up.\n"
                         "The %s monthly close, $%s, is ladder quantile <b>%.1f</b>, so the band is <b>%s</b> (it read %s on the old lines). No monthly close crossed a line.\n<b>Play:</b> %s" % (
                         LINES_TXT, LADDER_LINES[0], LADDER_LINES[0], LADDER_LINES[1], LADDER_LINES[1], LADDER_LINES[2], LADDER_LINES[2],
                         m_end.strftime("%b %Y"), format(round(m_px), ","), q_m, band_now, prev_band, BAND_PLAY[band_now]), sound="bike")
                    if band_now != prev_band: state["band_changed"] = now.isoformat()
                elif prev_band and band_now != prev_band:
                    line = BAND_LINE.get(band_now)
                    line_px = ladder_price(line, utc_now + timedelta(days=90)) if line else float("nan")
                    msg = ("The <b>%s</b> monthly close, $%s, is ladder quantile <b>%.1f</b>: the band moved from %s to <b>%s</b>.\n<b>Play:</b> %s%s\n"
                           "A monthly close crossed a band line: exit or rotate the primary into the new band's structure; the gate and the entry rules apply to the new long. Gate: BTC's close is %s its 50-day." % (
                           m_end.strftime("%b %Y"), format(round(m_px), ","), q_m, prev_band, band_now, BAND_PLAY[band_now],
                           (" The %d line in 90 days is BTC $%s; the WHAT IF box on the MSTX tab converts it." % (line, format(round(line_px), ","))) if line else "",
                           regime_now))
                    push("Ladder band: %s" % band_now, msg, sound="bike")
                    state["band_changed"] = now.isoformat()
                state["band"], state["band_q"], state["band_close"], state["band_lines"] = band_now, round(q_m, 1), m_end.strftime("%Y-%m-%d"), LINES_TXT
        except Exception as exc:
            failed('band', exc)
        try:
            btc_now = btc_close
            prev_regime = state.get("regime")
            if prev_regime in ("above", "below") and regime_now != prev_regime:
                band_txt = state.get("band") or "unknown"
                if regime_now == "above":
                    msg = ("BTC's daily close, $%s, is <b>above</b> its 50-day ($%s).\n<b>Gate:</b> Lag and Cheap alerts are on; Rich is muted.\n<b>Play:</b> long structures are allowed again. "
                           "Band on the monthly close: <b>%s</b>. %s Cheap swings and lag day trades are on." % (format(round(btc_now), ","), format(round(btc50), ","), band_txt, BAND_PLAY.get(band_txt, "")))
                else:
                    msg = ("BTC's daily close, $%s, is <b>below</b> its 50-day ($%s).\n<b>Gate:</b> Lag and Cheap alerts are muted; Rich is on.\n<b>Play:</b> no new money. "
                           "Roll the short call down and closer (30 to 45 days), keep the long. Close any open Cheap swing today." % (format(round(btc_now), ","), format(round(btc50), ",")))
                push("BTC %s its 50-day" % regime_now, msg, sound="bike")
                state["regime"] = regime_now; state["regime_changed"] = now.isoformat()
            elif prev_regime not in ("above", "below"):
                state["regime"] = regime_now
        except Exception as exc:
            failed('50-day crossing', exc)
        in_session = now.weekday() < 5 and (now.hour, now.minute) >= (9, 35) and (now.hour, now.minute) <= (16, 0)
        if not in_session and not FORCE:
            state["last_regime_check"] = now.isoformat()
            print("outside the regular session (%s NY); BTC %s its 50-day; nothing else to do" % (now.strftime("%a %H:%M"), regime_now)); return
        mstr_f = bars("MSTR", field=("Close", "Low"))          # the Low feeds the lag, so the monitor measures where the indicator measures
        mstr, mstr_lo, btc, mstx = mstr_f["Close"], mstr_f["Low"], bars("BTC-USD"), bars("MSTX")
        btc = btc.sort_index()
        btc_then = btc.copy(); btc_then.index = btc_then.index + pd.Timedelta(minutes=60)
        btc_hour_series = btc / btc_then.reindex(btc.index, method="ffill", tolerance=pd.Timedelta(minutes=5)) - 1   # a missing BTC minute must not blank the floor
        df = pd.concat([mstr.rename("MSTR"), mstr_lo.rename("MSTRLO"), btc.rename("BTC"), mstx.rename("MSTX")], axis=1, sort=False).sort_index()
        df["BTC"] = df["BTC"].ffill(); df["MSTX"] = df["MSTX"].ffill(); df = df.dropna()
        df["btc_hour"] = btc_hour_series.reindex(df.index, method="ffill", tolerance=pd.Timedelta(minutes=5))
        df = df[(df.index.time >= datetime.strptime("09:30", "%H:%M").time()) & (df.index.time <= datetime.strptime("16:00", "%H:%M").time())]
        score_ledger(df, ledger)
        last_day = df.index[-1].date()
        prev = df[df.index.date < last_day]                      # yesterday's last regular bar sets the MSTX mapping
        mstr_prev = float(prev["MSTR"].iloc[-1]) if len(prev) else float(df["MSTR"].iloc[0])
        mstx_prev = float(prev["MSTX"].iloc[-1]) if len(prev) else float(df["MSTX"].iloc[0])
        df = df[df.index.date == last_day]
        if len(df) < 20: print("only %d bars so far; waiting" % len(df)); return
        try:
            strc_close = yf.Ticker("STRC").history(period="5d")["Close"].dropna()
            strc = float(strc_close.iloc[-1]) if len(strc_close) else float(state["strc"])
        except Exception:
            if state.get("strc") is None:
                raise
            traceback.print_exc()
            strc = float(state["strc"])
        btc_last = float(df["BTC"].iloc[-1])
        df["target"] = [target(strc, b) for b in df["BTC"]]
        df["proj"] = BPS * df["BTC"] * df["target"]; df["gap"] = df["MSTR"] / df["proj"] - 1
        # Lag keeps its independent historical base and slope. Price levels use the fitted line.
        df["tgt_lag"] = [target(strc, b, LAG_SLOPE) for b in df["BTC"]]
        df["proj_lag"] = BPS * df["BTC"] * df["tgt_lag"]
        df["gap_lag"] = df["MSTR"] / df["proj_lag"] - 1
        # the lag is measured at MSTR's bar LOW, the same place mstx_projected.pine measures it. Measuring at the close
        # hid a real signal on 9/17: the chart printed its orange plus at 11:13 to 11:16 and the close-based reading never
        # reached the line. gap_lo is the same gap computed off the bar's low.
        df["gap_lo"] = df["MSTRLO"] / df["proj_lag"] - 1
        df["hour_avg"] = df["gap_lag"].shift(1).rolling(60, min_periods=30).mean()
        # Divide by the trailing average, do not subtract from it. Pine computes lev x (ratioLo / avR - 1); subtracting the
        # two gaps drops the denominator and reads differently whenever the gap is far from zero (1% off at a 1% gap, 10%
        # off at a 10% gap). Written this way the two tools agree at every level.
        df["lag"] = ((df["gap_lo"] if LAG_AT_LOW else df["gap_lag"]) - df["hour_avg"]) / (1 + df["hour_avg"])
        df["proj_x"] = mstx_prev * (1 + 2.0 * (df["proj"] / mstr_prev - 1))     # projected MSTX: yesterday's close moved 2x MSTR's projected move
        df["gap_x"] = df["MSTX"] / df["proj_x"] - 1
        r = df.iloc[-1]; t = df.index[-1]
        btc_hour = float(r["btc_hour"])
        regime = regime_now                                                       # the daily-close gate, the same one the crossing push uses
        mnav = r["MSTR"] / (btc_last * BPS)
        print("%s  MSTR %.2f  BTC %s  STRC %.2f  mNAV %.3f  target %.3f  projected %.2f  gap %+.2f%%  lag %s  BTC 1h %+.2f%%  BTC %s its 50-day  (inputs: %s)" % (
            t.strftime("%Y-%m-%d %H:%M"), r["MSTR"], format(round(btc_last), ","), strc, mnav, r["target"], r["proj"], 100 * r["gap"],
            ("%+.2f%%" % (100 * r["lag"])) if not math.isnan(r["lag"]) else "n/a", 100 * btc_hour, regime, cfg["_source"]))
        core = ("MSTX <b>$%.2f</b> vs projected <b>$%.2f</b> (gap <b>%+.1f%%</b>)\n"
                "MSTR $%.2f vs projected $%.2f (gap %+.1f%%, lag %s)\n"
                "BTC $%s (%+.1f%% last hour) · STRC $%.2f · mNAV %.3f vs target %.3f\n"
                "BTC is %s its 50-day ($%s) · holdings %s\n"
                "Ladder: <b>%s</b> band on the monthly close (%sq) · live %.1fq") % (r["MSTX"], r["proj_x"], 100 * r["gap_x"], r["MSTR"], r["proj"], 100 * r["gap"],
                ("%+.1f%%" % (100 * r["lag"])) if not math.isnan(r["lag"]) else "n/a", format(round(btc_last), ","), 100 * btc_hour, strc, mnav, r["target"], regime, format(round(btc50), ","), HOLD_SRC,
                state.get("band", "unknown"), state.get("band_q", "?"), ladder_q(btc_last, datetime.now(timezone.utc)))
        fired = []
        def fin(v):
            try: v = float(v)
            except Exception: return None
            return None if (math.isnan(v) or math.isinf(v)) else v
        def record(kind, muted=False, row=None, when=None):
            q = r if row is None else row; w = t if when is None else when       # a lag can trigger on a bar older than the newest one
            ledger.append({"kind": kind, "muted": muted, "time": w.isoformat(), "mstr": None if fin(q["MSTR"]) is None else round(fin(q["MSTR"]), 2), "btc": None if fin(q["BTC"]) is None else round(fin(q["BTC"]), 2), "proj": None if fin(q["proj"]) is None else round(fin(q["proj"]), 2),
                           "gap": None if fin(q["gap"]) is None else round(100 * fin(q["gap"]), 2), "lag": None if fin(q["lag"]) is None else round(100 * fin(q["lag"]), 2), "regime": regime,
                           "mstx": None if fin(q["MSTX"]) is None else round(fin(q["MSTX"]), 2), "proj_mstx": None if fin(q["proj_x"]) is None else round(fin(q["proj_x"]), 2), "gap_mstx": None if fin(q["gap_x"]) is None else round(100 * fin(q["gap_x"]), 2)})
        lag_t, lag_v = t, float("nan")  # watch remains safe if Lag evaluation fails
        # LAG. This runs every 5 minutes but the lag lives in single minutes, so judging only the newest bar looks at one
        # minute in five. On 9/17 the chart's trigger was met at 11:13, 11:14 and 11:16 and every one of those fell between
        # polls, so nothing was ever sent. Scan every bar since the previous run and judge the deepest one.
        try:
            lb = state.get("last_bar")
            scan = df[df.index > datetime.fromisoformat(lb)] if lb else df.iloc[0:0]
            if len(scan) > 30: scan = df.tail(90)
            elif len(scan) < 2: scan = df.tail(6)                 # first run of the day or no new bars
            scan = scan[scan["lag"].notna()].copy()
            # Judge every bar in the window, THEN take the deepest of the ones that qualify. Taking the deepest bar first and
            # testing it afterwards threw a real signal away whenever the deepest bar failed the BTC floor and a shallower bar
            # in the same window would have fired: a -2.0% lag with BTC down 2% masked a -1.4% lag with BTC flat.
            last_lag = state.get("last_lag_alert")
            cutoff = (datetime.fromisoformat(last_lag) + timedelta(minutes=60)) if last_lag else None
            elig = scan[(scan["lag"] <= LAG) & (scan["btc_hour"] >= BTC_HOLD)]
            if cutoff is not None: elig = elig[elig.index >= cutoff]
            if len(elig):
                lag_t, due = elig["lag"].idxmin(), True
            elif len(scan):
                lag_t, due = scan["lag"].idxmin(), False
            else:
                lag_t, due = t, False
            rl = df.loc[lag_t]
            lag_v = float(rl["lag"]) if not pd.isna(rl["lag"]) else float("nan")
            ago = "" if lag_t == t else " (that minute was %s, %d minutes back)" % (lag_t.strftime("%H:%M"), round((t - lag_t).total_seconds() / 60))
            if due and GATE and regime != "above":
                state["last_lag_alert"] = lag_t.isoformat(); record("lag", muted=True, row=df.loc[lag_t], when=lag_t); print("lag muted: BTC below its 50-day")
            elif due:
                push("Lag %+.1f%% MSTX" % (200 * lag_v),
                              core + "\n\n<b>LAG, day trade.</b> MSTR fell %.1f%% against the projection inside an hour with BTC holding (%.1f%% on MSTX)%s.\n<b>Play:</b> one long MSTX call at about 0.8 delta (nearest strike below the price), nearest expiry at least a day out, from the lag sleeve. Enter above the first green bar.\n<b>Exit:</b> +1.5%% on MSTX from entry or 60 minutes, whichever first. Stop under the dip low. Never hold to the close." % (100 * lag_v, 200 * lag_v, ago), sound="siren", priority=1)
                state["last_lag_alert"] = lag_t.isoformat(); fired.append("lag"); record("lag", row=df.loc[lag_t], when=lag_t)
        except Exception as exc:
            failed('Lag', exc)
        # CHEAP and RICH: fire on crossing the line, again when the gap moves a full point further, else at most once an hour while it holds
        def level_due(kind, gap_now, beyond):
            last_t = state.get("last_%s_alert" % kind); last_g = state.get("last_%s_gap" % kind)
            if not last_t or datetime.fromisoformat(last_t).date() != last_day: return True          # first time today
            if last_g is not None and beyond(gap_now, float(last_g)): return True                      # a full point further
            return (t - datetime.fromisoformat(last_t)) >= timedelta(minutes=60)                      # still there an hour later
        g = float(r["gap_x"])          # the MSTX gap, matching the indicator's cheap and rich lines
        try:
            if g <= CHEAP_X and level_due("cheap", g, lambda now, last: now <= last - 0.01) and GATE and regime != "above":
                state["last_cheap_alert"] = t.isoformat(); state["last_cheap_gap"] = g; record("cheap", muted=True); print("cheap muted: BTC below its 50-day")
            elif g <= CHEAP_X and level_due("cheap", g, lambda now, last: now <= last - 0.01):
                rule = ("<b>CHEAP, swing trade.</b> BTC is above its 50-day, the state where cheap closed with MSTR rising (+5.9% MSTR over 5 days in the backtest).\n<b>Play:</b> weekly call vertical from the swing sleeve: long just below the price, short at the projected price.\n<b>Exit:</b> when the gap closes to zero, or after 5 trading days, or the day BTC closes under its 50-day, whichever first." + ("\n<b>Primary:</b> the band is the sell zone; no new primary." if state.get("band") == "sell zone" else
                                "\n<b>Primary:</b> if this phase's primary is not on yet, Cheap is its entry day, at the phase's share of the sleeve: Phase 2 is the Jan/Dec diagonal at 70%; from Phase 3 it is the band's structure, long 12 months at 0.75 delta, short 90 days at the band ceiling.")
                        if regime == "above" else "<b>CHEAP, but BTC is below its 50-day.</b> The weaker state in the backtest; the gate is off, so this is context only.")
                push("Cheap %+.1f%% MSTX" % (100 * float(r["gap_x"])), core + "\n\n" + rule); state["last_cheap_alert"] = t.isoformat(); state["last_cheap_gap"] = g; fired.append("cheap"); record("cheap")
        except Exception as exc:
            failed('Cheap', exc)
        try:
            if g >= RICH_X and level_due("rich", g, lambda now, last: now >= last + 0.01) and RICH_GATE and regime != "below":
                state["last_rich_alert"] = t.isoformat(); state["last_rich_gap"] = g; record("rich", muted=True); print("rich muted: BTC above its 50-day")
            elif g >= RICH_X and level_due("rich", g, lambda now, last: now >= last + 0.01):
                # STRC's price and discount to par ride along as context. No verdict attached: the read that a cheap STRC
                # sharpens Rich was tested 9/17 and did not survive. It looked strong while STRC sat inside the target
                # formula (-5.94% vs -1.71% excess at 10 days), and reversed once the signal was rebuilt on raw mNAV with
                # no STRC in it (-4.36% vs -5.05%). The two extreme episodes went the wrong way too: STRC 88.22 was
                # followed by MSTR beating BTC by 8.2 points, STRC 99.74 by losing 16.1. The arbitrage is real economics;
                # it is not a detectable edge in fourteen months of price data.
                push("Rich %+.1f%% MSTX" % (100 * float(r["gap_x"])), core + "\n\n<b>RICH.</b> MSTX is ahead of projected. BTC is %s its 50-day. The fitted gap quartile sets this line. Rich is not a sell on its own.\n<b>STRC $%.2f, %+.1f%% to par.</b>\n<b>Play:</b> no new primary while Rich is on. A short vertical from the swing sleeve is optional; in the IBIT band Rich is the sell.\n<b>Exit:</b> when the gap returns to zero or after 5 trading days." % (regime, strc, strc - 100.0), sound="pushover"); state["last_rich_alert"] = t.isoformat(); state["last_rich_gap"] = g; fired.append("rich"); record("rich")
        except Exception as exc:
            failed('Rich', exc)
        try:
            # A near miss is data too. Without this the ledger only ever held alerts, so nothing that did not fire was on the
            # record and the file itself never got created (it 404'd to the site all week). At most one watch row per 30 minutes.
            if not fired and not math.isnan(lag_v) and lag_v <= LAG_WATCH:
                lw = state.get("last_watch_row")
                if not lw or (lag_t - datetime.fromisoformat(lw)) >= timedelta(minutes=30):
                    record("watch", row=df.loc[lag_t], when=lag_t); state["last_watch_row"] = lag_t.isoformat()
                    print("watch row logged: lag %+.2f%% MSTR (%+.2f%% MSTX) at %s, no alert" % (100 * lag_v, 200 * lag_v, lag_t.strftime("%H:%M")))
        except Exception as exc:
            failed('watch rows', exc)
        state.update({"last_run": now.isoformat(), "last_bar": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last), "strc": round(strc, 2),
                      "gap": round(100 * float(r["gap"]), 2), "lag": None if math.isnan(r["lag"]) else round(100 * float(r["lag"]), 2), "regime": regime, "fired": fired,
                      "mstx": round(float(r["MSTX"]), 2), "proj_mstx": round(float(r["proj_x"]), 2), "gap_mstx": round(100 * float(r["gap_x"]), 2),
                      "inputs": cfg["_source"], "holdings": HOLD_SRC, "btc_held": BTC_HELD, "shares_m": round(SHARES_M, 3),
                      # yesterday's closes, the base of the projected MSTX mapping, so the site never needs its own copy
                      "prev_day": prev.index[-1].date().isoformat() if len(prev) else None, "prev_mstr": round(mstr_prev, 2), "prev_mstx": round(mstx_prev, 2)})
        print("fired: %s" % (fired or "nothing"))
    loaded = set()
    try:
        state = load(STATE_FILE, {})
        loaded.add(STATE_FILE)
        ledger = load(LEDGER_FILE, [])
        loaded.add(LEDGER_FILE)
        run()
    except Exception as exc:
        failed("upstream", exc)
    finally:
        notify = False
        if errors:
            try:
                last_push = state.get("last_error_push")
                notify = (not last_push) or (now - datetime.fromisoformat(last_push)).total_seconds() >= 3600
            except Exception:
                notify = True
            if notify: state["last_error_push"] = now.isoformat()
        for path, value in ((STATE_FILE, state), (LEDGER_FILE, ledger)):
            if path not in loaded:
                continue
            try:
                with open(path, "w") as f:
                    json.dump(value, f, indent=2, allow_nan=False)
            except Exception as exc:
                failed("save " + path, exc)
        if errors and notify:
            try:
                push("Monitor error", "\n".join(error_messages))
            except Exception:
                traceback.print_exc()
    return 1 if errors else 0

if __name__ == "__main__":
    sys.exit(main())
