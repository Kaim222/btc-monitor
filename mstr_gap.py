"""
MSTR gap monitor: fires Pushover when MSTR trades away from Alex's projected price.

Projected MSTR = BTC x (BTC held / shares) x target mNAV, target = STRC rule + slope x (BTC - 75,000) / 2,500.
gap = MSTR / projected - 1.

Three alerts:
  LAG    the gap falls 1.5 points or more below its own average over the previous hour while BTC has held
         (BTC's own hour move better than -1%). In words: MSTR just dropped about 1.5% against the projection inside
         an hour and BTC did not. The minute-scale backtest (Jul-Sep 2026) shows these closing within 30 to 60 minutes.
         Cooldown 60 minutes.
  CHEAP  MSTR is under the cheap line vs projection (config, -3% MSTR = -6% MSTX). Fires on the cross, again on each full
         point further, and hourly while it holds. Carries BTC's 50-day state as context (not a gate).
  RICH   MSTR is over the rich line (config, +4% MSTR = +8% MSTX). Same cadence.
  BAND   the ladder band on the monthly close (Kaim power-law quantile: under 15 MSTX, 15 to 50 MSTR, 50 to 85 IBIT, 85 and up
         the sell zone), pushed when a month's close moves it. The 50-day crossing, Cheap and Rich pushes carry the ladder plays
         (the site's data/ladder-rules.json is the written version).
  Every alert leads with MSTX vs projected MSTX (yesterday's close moved 2x MSTR's projected move), then MSTR.
  Regime gate (config regime_gate, default on): Lag and Cheap push only with BTC above its 50-day; Rich only below. Muted alerts are still logged and scored.

Holdings and the assumed diluted share count come from api.strategy.com/btc/bitcoinKpis on every run (btcHoldings and
satsPerShare; this reproduces strategy.com/shares' ADSO exactly), so Monday's 8-K flows through by itself. Thresholds and the
slope come from the ladder site's data/mstr-config.json (fetched live from GitHub; editing that file changes both the site and
this monitor); set btc_held or shares_m there only to override the API. A local mstr_config.json is the fallback. State in mstr_state.json. Every alert is scored on later
runs (MSTR minus BTC, and MSTX itself, over the next 30 and 60 minutes) into mstr_ledger.json, so the rule keeps a record of itself.
Regular session only (9:35 to 16:00 New York). Env: PUSHOVER_TOKEN, PUSHOVER_USER; without them it prints instead of
sending. Flags: --force (run outside market hours on the last session's bars), --test (send one test message).
"""
import os, sys, json, math, urllib.request, urllib.parse
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
        with urllib.request.urlopen(req, timeout=10) as r: cfg = json.loads(r.read()); cfg["_source"] = "ladder site"; return cfg
    except Exception as e:
        print("config from the ladder site failed (%s); using the local file" % e)
        cfg = load(CONFIG_FILE, {}); cfg["_source"] = "local"; return cfg
cfg = load_config()
STRATEGY_API = "https://api.strategy.com/btc/bitcoinKpis"
def strategy_holdings(state):
    """(btc_held, shares_m, as_of, source). Live from strategy.com; else the last good values in state; else the config."""
    try:
        req = urllib.request.Request(STRATEGY_API, headers={"User-Agent": "Mozilla/5.0 mstr-gap-monitor"})
        with urllib.request.urlopen(req, timeout=10) as r: k = json.loads(r.read())["results"]
        held = float(str(k["btcHoldings"]).replace(",", "")); sps = float(k["satsPerShare"])
        shares_m = held / (sps / 1e8) / 1e6
        if held > 100000 and 100 < shares_m < 5000:
            state["strategy_last"] = {"btc_held": held, "shares_m": round(shares_m, 3), "as_of": k.get("msTimestamp"), "fetched": datetime.now(NY).isoformat()}
            return held, shares_m, "strategy.com live"
    except Exception as e:
        print("strategy.com holdings failed (%s)" % e)
    s = state.get("strategy_last")
    if s: return float(s["btc_held"]), float(s["shares_m"]), "strategy.com cached %s" % s.get("fetched", "")[:16]
    return None, None, "none"
_state0 = load(STATE_FILE, {})
_prev = dict(_state0.get("strategy_last") or {})
_h, _s, HOLD_SRC = strategy_holdings(_state0)
PINE_FILES = ["mstr_gap_lag.pine", "mstr_projected.pine", "mstx_projected.pine"]
def sync_pine(held, shares_m):
    """Rewrite the TradingView indicators' two default inputs so a re-paste carries the new holdings. Returns True if any changed."""
    import re
    changed = False
    for pf in PINE_FILES:
        if not os.path.exists(pf): continue
        src = open(pf, encoding="utf-8").read()
        new = re.sub(r'input\.float\([0-9.]+, "BTC held"', 'input.float(%d, "BTC held"' % int(round(held)), src)
        new = re.sub(r'input\.float\([0-9.]+, "Assumed diluted shares \(M\)"', 'input.float(%.3f, "Assumed diluted shares (M)"' % shares_m, new)
        if new != src:
            open(pf, "w", encoding="utf-8").write(new); changed = True
    return changed
if cfg.get("btc_held") not in (None, "", "auto"): _h, HOLD_SRC = float(cfg["btc_held"]), "config override"
if cfg.get("shares_m") not in (None, "", "auto"): _s = float(cfg["shares_m"]); HOLD_SRC = "config override"
BTC_HELD = _h if _h else 845050.0; SHARES_M = _s if _s else 450.112
SLOPE = float(cfg.get("btc_slope_per_2500", 0.0125))
LAG, CHEAP, RICH = float(cfg.get("lag_threshold", -0.015)), float(cfg.get("cheap_threshold", -0.04)), float(cfg.get("rich_threshold", 0.04))
BTC_HOLD = float(cfg.get("btc_hour_move_floor", -0.01))
GATE = bool(cfg.get("regime_gate", True))     # Lag and Cheap push only with BTC above its 50-day; Rich only below. Muted ones are still logged.
BPS = BTC_HELD / (SHARES_M * 1e6)

def strc_rule(s):
    if s >= 97.5: return 0.90
    if s >= 95: return 0.875 + (s - 95) * 0.01
    if s >= 92.5: return 0.85 + (s - 92.5) * 0.01
    if s >= 87.5: return 0.825 + (s - 87.5) * 0.005
    if s >= 82.5: return 0.80 + (s - 82.5) * 0.005
    return max(0.775, 0.775 + (s - 77.5) * 0.005)
def target(strc, btc): return strc_rule(strc) + SLOPE * (btc - 75000) / 2500

# The ladder: Kaim power law (A 5.82, B -17.029 on days since 2009-01-03) and its quantile bands, the same constants as the site.
_GEN = datetime(2009, 1, 3, tzinfo=timezone.utc)
_BANDS = [(99.9, -0.0000756204, 0.7434), (95, -0.0000583518, 0.5943), (85, -0.0000516698, 0.4318), (50, 0, -0.0004), (15, 0, -0.2092), (0.1, 0, -0.3403)]
def _days(ts):
    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
    return (ts - _GEN).total_seconds() / 86400
def _band_offsets(ts):
    d, today = _days(ts), _days(datetime.now(timezone.utc))
    return [(q, m * (min(d, today) if m < 0 else d) + c) for q, m, c in _BANDS]    # the upper bands decay, capped at today
def fair_value(ts): return 10 ** (5.82 * math.log10(_days(ts)) - 17.029)
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
    return float("nan")
def ladder_band(q): return "MSTX" if q < 15 else "MSTR" if q < 50 else "IBIT" if q < 85 else "sell zone"
BAND_LINE = {"MSTX": 15, "MSTR": 50, "IBIT": 85}          # the band ceiling, where the short goes and the rotation triggers
BAND_PLAY = {"MSTX": "MSTX PMCC: long 12 months at 0.75 delta, short 90 days at the 15 line, rolled.",
             "MSTR": "MSTR PMCC: long 12 months at 0.75 delta, short 90 days at the 50 line, rolled.",
             "IBIT": "IBIT PMCC: long 12 months at 0.75 delta, short 90 days at the 85 line, rolled. Rich readings here are the sell.",
             "sell zone": "Sell the BTC beta into it and rotate down; the proceeds sit in STRC."}

def send_pushover(title, message, sound="cashregister"):
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not token or not user:
        print("[dry run, no Pushover keys]\n" + title + "\n" + message.replace("<b>", "").replace("</b>", "")); return
    data = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": message, "html": "1", "sound": sound}).encode()
    req = urllib.request.Request("https://api.pushover.net/1/messages.json", data=data)
    with urllib.request.urlopen(req, timeout=10) as r: result = json.loads(r.read())
    if result.get("status") != 1: raise RuntimeError("Pushover error: %s" % result)
    print("Pushover sent: " + title)

# the Monday check: needs send_pushover, so it lives below it
if _h and _s and "override" not in HOLD_SRC:
    changed = sync_pine(_h, _s)
    moved = _prev and (abs(float(_prev.get("btc_held", 0)) - _h) >= 1 or abs(float(_prev.get("shares_m", 0)) - _s) >= 0.001)
    if moved:
        send_pushover("Holdings changed", "Strategy now shows <b>%s BTC</b> over <b>%.3fM</b> assumed diluted shares (was %s / %.3fM). The site and this monitor already use the new numbers. Type the two numbers into the TradingView indicator's settings (or re-paste mstx_projected.pine from the monitor repo, its defaults are updated)." % (
            format(int(_h), ","), _s, format(int(float(_prev.get("btc_held", 0))), ","), float(_prev.get("shares_m", 0))), sound="magic")

def bars(ticker, interval="1m", period="2d"):   # BTC "1d" is the UTC day and goes empty after 8 PM New York, so two days
    h = yf.Ticker(ticker).history(period=period, interval=interval, prepost=False)
    if h.empty: raise RuntimeError("no %s bars for %s" % (interval, ticker))
    h.index = h.index.tz_convert(NY); return h["Close"]

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
    state, ledger = load(STATE_FILE, {}), load(LEDGER_FILE, [])
    if _state0.get("strategy_last"): state["strategy_last"] = _state0["strategy_last"]
    if TEST:
        send_pushover("Test", "Wired. Holdings %s (%s): BTC held %s, shares %.3fM. Thresholds from the %s: slope %.4f per $2,500, lag %.1f%% inside an hour, cheap %.0f%%, rich +%.0f%%." % (
            HOLD_SRC, "auto" if "override" not in HOLD_SRC else "manual", format(int(BTC_HELD), ","), SHARES_M, cfg["_source"], SLOPE, 100 * LAG, 100 * CHEAP, 100 * RICH))
        with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
        return
    # BTC's daily close vs its 50-day, and the ladder band on the monthly close: checked on every run, in or out of the session, pushed on a change.
    # Completed UTC days only (the rule is the daily close, so the crossing fires once, on the first run after the close, never on a wick).
    btc_daily = yf.Ticker("BTC-USD").history(period="130d")["Close"].dropna()
    utc_now = datetime.now(timezone.utc); tz = btc_daily.index.tz
    done = btc_daily[btc_daily.index < pd.Timestamp(utc_now.year, utc_now.month, utc_now.day, tz=tz)]
    btc50 = float(done.tail(50).mean()); btc_close = float(done.iloc[-1])
    regime_now = "above" if btc_close > btc50 else "below"
    # the band: the prior month's last daily close, run through the ladder at the month-end instant; a touch on the daily is not a rotation
    m0 = pd.Timestamp(utc_now.year, utc_now.month, 1, tz=tz); mclose = btc_daily[btc_daily.index < m0]
    if len(mclose) and (m0 - mclose.index[-1]) <= pd.Timedelta(days=1):
        m_end, m_px = (m0 - pd.Timedelta(milliseconds=1)).to_pydatetime(), float(mclose.iloc[-1])
        q_m = ladder_q(m_px, m_end); band_now = ladder_band(q_m); prev_band = state.get("band")
        if prev_band and band_now != prev_band:
            line = BAND_LINE.get(band_now)
            line_px = ladder_price(line, utc_now + timedelta(days=90)) if line else float("nan")
            msg = ("The <b>%s</b> monthly close, $%s, is ladder quantile <b>%.1f</b>: the band moved from %s to <b>%s</b>.\n<b>Play:</b> %s%s\n"
                   "A monthly close crossed a band line: exit or rotate the primary into the new band's structure; the gate and the entry rules apply to the new long. Gate: BTC's close is %s its 50-day." % (
                   m_end.strftime("%b %Y"), format(round(m_px), ","), q_m, prev_band, band_now, BAND_PLAY[band_now],
                   (" The %d line in 90 days is BTC $%s; the WHAT IF box on the MSTX tab converts it." % (line, format(round(line_px), ","))) if line else "",
                   regime_now))
            send_pushover("Ladder band: %s" % band_now, msg, sound="bike")
            state["band_changed"] = now.isoformat()
        state["band"], state["band_q"], state["band_close"] = band_now, round(q_m, 1), m_end.strftime("%Y-%m-%d")
        with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)      # saved now, so a later fetch failure cannot repeat the push
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
        send_pushover("BTC %s its 50-day" % regime_now, msg, sound="bike")
        state["regime"] = regime_now; state["regime_changed"] = now.isoformat()
        with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
    elif prev_regime not in ("above", "below"):
        state["regime"] = regime_now
    in_session = now.weekday() < 5 and (now.hour, now.minute) >= (9, 35) and (now.hour, now.minute) <= (16, 0)
    if not in_session and not FORCE:
        state["last_regime_check"] = now.isoformat()
        with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
        print("outside the regular session (%s NY); BTC %s its 50-day; nothing else to do" % (now.strftime("%a %H:%M"), regime_now)); return
    mstr, btc, mstx = bars("MSTR"), bars("BTC-USD"), bars("MSTX")
    df = pd.concat([mstr.rename("MSTR"), btc.rename("BTC"), mstx.rename("MSTX")], axis=1)
    df["BTC"] = df["BTC"].ffill(); df["MSTX"] = df["MSTX"].ffill(); df = df.dropna()
    df = df[(df.index.time >= datetime.strptime("09:30", "%H:%M").time()) & (df.index.time <= datetime.strptime("16:00", "%H:%M").time())]
    if score_ledger(df, ledger):
        with open(LEDGER_FILE, "w") as f: json.dump(ledger, f, indent=2)
    last_day = df.index[-1].date()
    prev = df[df.index.date < last_day]                      # yesterday's last regular bar sets the MSTX mapping
    mstr_prev = float(prev["MSTR"].iloc[-1]) if len(prev) else float(df["MSTR"].iloc[0])
    mstx_prev = float(prev["MSTX"].iloc[-1]) if len(prev) else float(df["MSTX"].iloc[0])
    df = df[df.index.date == last_day]
    if len(df) < 20: print("only %d bars so far; waiting" % len(df)); return
    strc = float(yf.Ticker("STRC").history(period="5d")["Close"].dropna().iloc[-1])
    btc_last = float(df["BTC"].iloc[-1])
    df["target"] = [target(strc, b) for b in df["BTC"]]
    df["proj"] = BPS * df["BTC"] * df["target"]; df["gap"] = df["MSTR"] / df["proj"] - 1
    df["hour_avg"] = df["gap"].shift(1).rolling(60, min_periods=30).mean(); df["lag"] = df["gap"] - df["hour_avg"]
    df["proj_x"] = mstx_prev * (1 + 2.0 * (df["proj"] / mstr_prev - 1))     # projected MSTX: yesterday's close moved 2x MSTR's projected move
    df["gap_x"] = df["MSTX"] / df["proj_x"] - 1
    r = df.iloc[-1]; t = df.index[-1]
    btc_hour = btc_last / float(df["BTC"].iloc[max(0, len(df) - 61)]) - 1
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
    today = str(last_day); fired = []
    def record(kind, muted=False):
        ledger.append({"kind": kind, "muted": muted, "time": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last, 2), "proj": round(float(r["proj"]), 2),
                       "gap": round(100 * float(r["gap"]), 2), "lag": None if math.isnan(r["lag"]) else round(100 * float(r["lag"]), 2), "regime": regime,
                       "mstx": round(float(r["MSTX"]), 2), "proj_mstx": round(float(r["proj_x"]), 2), "gap_mstx": round(100 * float(r["gap_x"]), 2)})
    # LAG
    last_lag = state.get("last_lag_alert")
    cool = last_lag and (t - datetime.fromisoformat(last_lag)) < timedelta(minutes=60)
    if not math.isnan(r["lag"]) and r["lag"] <= LAG and btc_hour >= BTC_HOLD and not cool and GATE and regime != "above":
        state["last_lag_alert"] = t.isoformat(); record("lag", muted=True); print("lag muted: BTC below its 50-day")
    elif not math.isnan(r["lag"]) and r["lag"] <= LAG and btc_hour >= BTC_HOLD and not cool:
        send_pushover("Lag %+.1f%% MSTX" % (200 * r["lag"]),
                      core + "\n\n<b>LAG, day trade.</b> MSTR fell %.1f%% against the projection inside an hour with BTC holding (%.1f%% on MSTX).\n<b>Play:</b> one long MSTX call at about 0.8 delta (nearest strike below the price), nearest expiry at least a day out, from the lag sleeve. Enter above the first green bar.\n<b>Exit:</b> +1.5%% on MSTX from entry or 60 minutes, whichever first. Stop under the dip low. Never hold to the close." % (100 * r["lag"], 200 * r["lag"]), sound="siren")
        state["last_lag_alert"] = t.isoformat(); fired.append("lag"); record("lag")
    # CHEAP and RICH: fire on crossing the line, again when the gap moves a full point further, else at most once an hour while it holds
    def level_due(kind, gap_now, beyond):
        last_t = state.get("last_%s_alert" % kind); last_g = state.get("last_%s_gap" % kind)
        if not last_t or datetime.fromisoformat(last_t).date() != last_day: return True          # first time today
        if last_g is not None and beyond(gap_now, float(last_g)): return True                      # a full point further
        return (t - datetime.fromisoformat(last_t)) >= timedelta(minutes=60)                      # still there an hour later
    g = float(r["gap"])
    if g <= CHEAP and level_due("cheap", g, lambda now, last: now <= last - 0.01) and GATE and regime != "above":
        state["last_cheap_alert"] = t.isoformat(); state["last_cheap_gap"] = g; record("cheap", muted=True); print("cheap muted: BTC below its 50-day")
    elif g <= CHEAP and level_due("cheap", g, lambda now, last: now <= last - 0.01):
        rule = ("<b>CHEAP, swing trade.</b> BTC is above its 50-day, the state where cheap closed with MSTR rising (+5.9% MSTR over 5 days in the backtest).\n<b>Play:</b> weekly call vertical from the swing sleeve: long just below the price, short at the projected price.\n<b>Exit:</b> when the gap closes to zero, or after 5 trading days, or the day BTC closes under its 50-day, whichever first." + ("\n<b>Primary:</b> the band is the sell zone; no new primary." if state.get("band") == "sell zone" else
                        "\n<b>Primary:</b> if this phase's primary is not on yet, Cheap is its entry day, at the phase's share of the sleeve: Phase 2 is the Jan/Dec diagonal at 70%; from Phase 3 it is the band's structure, long 12 months at 0.75 delta, short 90 days at the band ceiling.")
                if regime == "above" else "<b>CHEAP, but BTC is below its 50-day.</b> The weaker state in the backtest; the gate is off, so this is context only.")
        send_pushover("Cheap %+.1f%% MSTX" % (100 * float(r["gap_x"])), core + "\n\n" + rule); state["last_cheap_alert"] = t.isoformat(); state["last_cheap_gap"] = g; fired.append("cheap"); record("cheap")
    if g >= RICH and level_due("rich", g, lambda now, last: now >= last + 0.01) and GATE and regime != "below":
        state["last_rich_alert"] = t.isoformat(); state["last_rich_gap"] = g; record("rich", muted=True); print("rich muted: BTC above its 50-day")
    elif g >= RICH and level_due("rich", g, lambda now, last: now >= last + 0.01):
        send_pushover("Rich %+.1f%% MSTX" % (100 * float(r["gap_x"])), core + "\n\n<b>RICH, sell.</b> BTC is %s its 50-day. Rich readings faded about 2%% vs BTC over five days in the backtest.\n<b>Play:</b> sell what you hold, or a short vertical from the swing sleeve. No new primary while Rich is on; in the IBIT band Rich is the sell.\n<b>Exit:</b> when the gap returns to zero or after 5 trading days." % regime, sound="pushover"); state["last_rich_alert"] = t.isoformat(); state["last_rich_gap"] = g; fired.append("rich"); record("rich")
    if fired:
        with open(LEDGER_FILE, "w") as f: json.dump(ledger, f, indent=2)
    state.update({"last_run": now.isoformat(), "last_bar": t.isoformat(), "mstr": round(float(r["MSTR"]), 2), "btc": round(btc_last), "strc": round(strc, 2),
                  "gap": round(100 * float(r["gap"]), 2), "lag": None if math.isnan(r["lag"]) else round(100 * float(r["lag"]), 2), "regime": regime, "fired": fired,
                  "mstx": round(float(r["MSTX"]), 2), "proj_mstx": round(float(r["proj_x"]), 2), "gap_mstx": round(100 * float(r["gap_x"]), 2),
                  "inputs": cfg["_source"], "holdings": HOLD_SRC, "btc_held": BTC_HELD, "shares_m": round(SHARES_M, 3)})
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)
    print("fired: %s" % (fired or "nothing"))

if __name__ == "__main__":
    main()
