# BTC / MSTR monitor

`mstr_gap.py` checks the market session every five minutes when GitHub schedules it;
completed daily BTC closes and monthly band changes are also checked around the clock.

Alerts:
- Lag: day-trade signal, siren at priority 1; BTC 50-day regime gate applies.
- Cheap: swing signal; BTC 50-day regime gate applies.
- Rich: sell signal; the live `rich_gate` switch controls its mute branch.
- Ladder band: rotation on the prior monthly close, never a live-spot touch.
- BTC 50-day crossing: notification when the completed daily-close regime flips.
- Holdings changed: Strategy holdings/share-count update, priority -1.
- Monitor error: one summary of the failed blocks, at most once an hour; a run with a failed block exits with status 1.
- Monitor down: workflow failure with run URL, only after a successful completed run.
- Test (`--test`): siren at priority 1, exercising Lag's delivery settings.
Watch rows are ledger-only observations, with no push.

Files:
- `mstr_gap.py`: data fetches, isolated alerts, retrying delivery, and final state/ledger saves.
- `mstr_config.json`: local fallback for the live signal configuration.
- `mstr_state.json`: active state and cached holdings/STRC.
- `mstr_ledger.json`: signal observations and later performance scores.
- `merge_ledger.py`: merges concurrent ledger rows, filling null fields and retaining first-seen non-null values.
- `mstr_gap_lag.pine`, `mstr_projected.pine`, `mstx_projected.pine`: TradingView indicators.
- `.github/workflows/mstr_gap.yml`: active schedule, state persistence, and failure notification.
- `test_monitor.py`: offline reliability regression tests (`python -m pytest -q test_monitor.py`).
- `AUDIT-alerts.json`: alert audit; signal-change recommendations are not implemented by this reliability pass.
- `consider-deleting/`: retired monitor, backup, state files, and inactive workflow; see its README.

The 60-bar trailing average (minimum 30), thresholds, slopes, cooldowns, and mute rules are unchanged.
BTC's hourly change uses its own continuous series before joining market bars.
Delivery retries three times; exhausted delivery failures are reported as block failures.
