"""Offline fixtures for warning math, source handling and alert delivery."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock, patch

import warning_signs as monitor

NOW = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
TODAY = int(NOW.replace(hour=0).timestamp())
LAST = TODAY - monitor.DAY


def prices(value=D(100), count=300):
    return {LAST - i * monitor.DAY: value for i in range(count)}


def fixtures():
    closes = prices()
    cb = [[stamp, 99, 101, 100, str(close), 10] for stamp, close in closes.items()]
    spot = [[str(stamp * 1000), '100', '101', '99', str(close), '10', '10', '10', '1']
            for stamp, close in list(closes.items())[:30]]
    settled = TODAY + 8 * 3600
    rates = [{"fundingTime": str((settled - i * 8 * 3600) * 1000), "fundingRate": "0.0002"}
             for i in range(25)]
    oi = [[str((TODAY - i * monitor.DAY + 16 * 3600) * 1000), '999', '500', str(110 - i)] for i in range(9)]
    return {monitor.COINBASE: cb, monitor.SPOT: spot, monitor.FUNDING: rates, monitor.OI: oi}


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(monitor.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_moving_average_thresholds(self):
        for flag, days, threshold in [('above50', 50, D('1.3')), ('above20', 20, D('1.2')),
                                       ('mayer', 200, D('1.5'))]:
            for delta, expected in [(D('-0.00001'), False), (D(0), True), (D('0.00001'), True)]:
                with self.subTest(flag=flag, delta=delta):
                    # This includes today's last completed close in the SMA.
                    closes = prices(D(days) - threshold)
                    closes[LAST] = threshold * (days - 1) + delta
                    self.assertEqual(monitor.price_flag(flag, closes, LAST)[1], expected)

    def test_gain30_threshold(self):
        for close, expected in [('149.999', False), ('150', True), ('150.001', True)]:
            closes = prices()
            closes[LAST] = D(close)
            self.assertEqual(monitor.price_flag('gain30', closes, LAST)[1], expected)

    def test_funding_annual_threshold_and_oi_comparison(self):
        source = fixtures()
        rates, oi = source[monitor.FUNDING], source[monitor.OI]
        # Set the sum directly to avoid rounding a repeating per-payment decimal.
        for annual, expected in [('0.149999', False), ('0.15', True), ('0.150001', True)]:
            for row in rates: row['fundingRate'] = '0'
            rates[2]['fundingRate'] = str(D(annual) * 7 / 365)
            self.assertEqual(monitor.funding_flag(rates, oi, prices(), LAST, NOW)[1], expected)
        for row in rates: row['fundingRate'] = '0.0002'
        oi[8][3] = '100'
        for current, expected in [('99.999', False), ('100', False), ('100.001', True)]:
            oi[1][3] = current
            self.assertEqual(monitor.funding_flag(rates, oi, prices(), LAST, NOW)[1], expected)

    def test_oi_history_uses_usd_and_completed_days(self):
        source = fixtures()
        oi = source[monitor.OI]
        oi[0][3] = '0'  # Current UTC day must not participate.
        oi[1][3], oi[8][3] = '120', '100'
        value, on = monitor.funding_flag(source[monitor.FUNDING], oi, prices(), LAST, NOW)
        self.assertTrue(on)
        self.assertEqual(value, '21.9%, OI +20.0%')
        oi[1][3] = '90'
        self.assertFalse(monitor.funding_flag(source[monitor.FUNDING], oi, prices(), LAST, NOW)[1])

    def test_current_day_funding_cannot_change_flag(self):
        for rate, expected in [('0', False), ('0.0002', True)]:
            source = fixtures()
            rates = source[monitor.FUNDING]
            for row in rates: row['fundingRate'] = rate
            baseline = monitor.funding_flag(rates, source[monitor.OI], prices(), LAST, NOW)
            self.assertEqual(baseline[1], expected)
            for extreme in ('999', '-999', 'NaN'):
                for row in rates:
                    if int(row['fundingTime']) >= TODAY * 1000: row['fundingRate'] = extreme
                self.assertEqual(monitor.funding_flag(rates, source[monitor.OI], prices(), LAST, NOW), baseline)
            completed = [r for r in rates if int(r['fundingTime']) < TODAY * 1000]
            self.assertEqual(monitor.funding_flag(completed, source[monitor.OI], prices(), LAST, NOW), baseline)

    def test_corrupt_or_unreadable_state_is_silent_first_run(self):
        report = monitor.collect(NOW, fixtures().__getitem__)
        for contents in ('{broken', '[]', '{"funding": "bad"}', b'\xff'):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / 'warning_state.json'
                state.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
                with patch.object(monitor, 'STATE_FILE', state), patch.object(monitor, 'WARNINGS_FILE', Path(tmp) / 'warnings.json'), patch.object(monitor, 'collect', return_value=report), patch.object(monitor, 'send_pushover') as send:
                    monitor.main()
                    self.assertEqual(json.loads(state.read_text()), {f['id']: f['on'] for f in report['flags']})
                    send.assert_not_called()
        with patch('builtins.open', side_effect=PermissionError('unreadable')), patch.object(monitor, 'collect', return_value=report), patch.object(monitor, 'save') as save, patch.object(monitor, 'send_pushover') as send:
            monitor.main()
            self.assertEqual(save.call_args.args[1], {f['id']: f['on'] for f in report['flags']})
            send.assert_not_called()

    def test_premium_strict_threshold(self):
        for close, expected in [('998.999', True), ('999', False), ('999.001', False)]:
            cb = prices(D(960))
            for i in range(3): cb[LAST - i * monitor.DAY] = D(close)
            self.assertEqual(monitor.premium_flag(cb, prices(D(1000)), LAST)[1], expected)

    def test_premium_price_change_strict_threshold(self):
        for close, expected in [('1029.999', False), ('1030', False), ('1030.001', True)]:
            cb = prices(D(1000))
            for i in range(3): cb[LAST - i * monitor.DAY] = D(close)
            self.assertEqual(monitor.premium_flag(cb, prices(D(1040)), LAST)[1], expected)

    def test_premium_averages_three_matching_days(self):
        cb, spot = prices(D(999)), prices(D(1000))
        cb[LAST - 7 * monitor.DAY] = D(950)
        cb[LAST] = D(997)
        cb[LAST - monitor.DAY] = D(1000)
        self.assertTrue(monitor.premium_flag(cb, spot, LAST)[1])
        del spot[LAST - monitor.DAY]
        with self.assertRaises(ValueError): monitor.premium_flag(cb, spot, LAST)

    def test_completed_utc_candles_only(self):
        source = fixtures()
        source[monitor.COINBASE].insert(0, [TODAY, 1, 1, 1, '999999', 1])
        source[monitor.SPOT].insert(0, [str(TODAY * 1000), '1', '1', '1', '999999', '1', '1', '1', '0'])
        report = monitor.collect(NOW, source.__getitem__)
        self.assertEqual(report['btc'], 100)
        self.assertEqual(report['as_of'], '2026-10-04')
        self.assertEqual(report['auto_count'], 6)
        self.assertEqual(report['on_count'], 1)
        self.assertEqual(len(report['flags']), 8)
        for flag in report['flags'][-2:]:
            self.assertIsNone(flag['on'])
            self.assertEqual(flag['note'], 'not automated')

    def test_unconfirmed_okx_candle_is_unavailable(self):
        source = fixtures()
        source[monitor.SPOT][0][8] = '0'
        report = monitor.collect(NOW, source.__getitem__)
        self.assertIsNone(report['flags'][5]['on'])

    def test_failed_sources_are_isolated(self):
        for failed, affected in [(monitor.COINBASE, 6), (monitor.FUNDING, 1), (monitor.OI, 1), (monitor.SPOT, 1)]:
            with self.subTest(source=failed):
                source = fixtures()
                def fetch(url):
                    if url == failed: raise OSError('fixture outage')
                    return source[url]
                report = monitor.collect(NOW, fetch)
                self.assertEqual(report['auto_count'], 6 - affected)
                for flag in report['flags'][:6]:
                    if flag['on'] is None: self.assertIn('fixture outage', flag['note'])
                if failed == monitor.COINBASE:
                    self.assertIsNone(report['btc'])
                    self.assertIsNone(report['as_of'])

    def test_short_gapped_and_stale_history(self):
        for count in (19, 49, 199):
            with self.assertRaises(ValueError): monitor.price_flag('mayer', prices(count=count), LAST)
        closes = prices()
        del closes[LAST - 10 * monitor.DAY]
        with self.assertRaises(ValueError): monitor.price_flag('gain30', closes, LAST)
        source = fixtures()
        source[monitor.COINBASE].pop(0)
        report = monitor.collect(NOW, source.__getitem__)
        self.assertEqual(report['auto_count'], 0)

    def test_source_exception_without_message(self):
        def fetch(url): raise TimeoutError()
        report = monitor.collect(NOW, fetch)
        self.assertEqual(report['auto_count'], 0)
        self.assertIn('TimeoutError', report['flags'][0]['note'])

    def test_http_retry_and_api_error(self):
        def response(payload):
            result = Mock()
            result.__enter__ = Mock(return_value=io.StringIO(json.dumps(payload)))
            result.__exit__ = Mock(return_value=False)
            return result
        with patch.object(monitor.time, 'sleep'), patch.object(monitor.urllib.request, 'urlopen',
                side_effect=[OSError('offline'), response({'code': '500', 'data': []}),
                             response({'code': '0', 'data': [['fixture']]})]) as request:
            self.assertEqual(monitor.get_json(monitor.OI), [['fixture']])
            self.assertEqual(request.call_count, 3)
        with patch.object(monitor.time, 'sleep'), patch.object(monitor.urllib.request, 'urlopen',
                side_effect=lambda *args, **kwargs: response({'code': '500', 'data': []})):
            with self.assertRaisesRegex(ValueError, 'API error 500'): monitor.get_json(monitor.OI)

    def test_funding_missing_stale_zero_and_nonfinite_data(self):
        for fault in ('rate_gap', 'rate_stale', 'oi_gap', 'oi_stale', 'oi_zero', 'oi_nan'):
            with self.subTest(fault=fault):
                source = fixtures()
                rates, oi = source[monitor.FUNDING], source[monitor.OI]
                if fault == 'rate_gap': rates.pop(5)
                if fault == 'rate_stale': rates.pop(2)
                if fault == 'oi_gap': oi.pop(8)
                if fault == 'oi_stale': del oi[:2]
                if fault == 'oi_zero': oi[1][3] = '0'
                if fault == 'oi_nan': oi[1][3] = 'NaN'
                report = monitor.collect(NOW, source.__getitem__)
                self.assertIsNone(report['flags'][4]['on'])
                self.assertIn('note', report['flags'][4])
                self.assertEqual(report['auto_count'], 5)

    def test_first_run_unchanged_and_both_transitions(self):
        report = monitor.collect(NOW, fixtures().__getitem__)
        send = Mock(return_value=True)
        state = monitor.update_state(report, None, send)
        send.assert_not_called()
        self.assertEqual(monitor.update_state(report, state, send), state)
        send.assert_not_called()
        report['flags'][0]['on'] = True
        report['flags'][4]['on'] = False
        next_state = monitor.update_state(report, state, send)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args_list[0].args, ('Bitcoin warning sign',
            'BTC 30%+ over 50-day avg: 0.0% (threshold 30%). 1 of 6 on.'))
        self.assertEqual(send.call_args_list[0].kwargs, {'priority': 0})
        self.assertEqual(send.call_args_list[1].args, ('Bitcoin warning cleared',
            report['flags'][4]['label'] + ': ' + report['flags'][4]['value'] + '.'))
        monitor.update_state(report, next_state, send)
        self.assertEqual(send.call_count, 2)

    def test_outage_preserves_state_and_recovery_detects_change(self):
        report = monitor.collect(NOW, fixtures().__getitem__)
        send = Mock(return_value=True)
        state = monitor.update_state(report, None, send)
        report['flags'][0]['on'] = None
        self.assertEqual(monitor.update_state(report, state, send)['above50'], False)
        send.assert_not_called()
        report['flags'][0]['on'] = True
        self.assertTrue(monitor.update_state(report, state, send)['above50'])
        send.assert_called_once()

    def test_first_valid_reading_after_initial_outage_is_baseline(self):
        report = monitor.collect(NOW, fixtures().__getitem__)
        send = Mock(return_value=True)
        report['flags'][0]['on'] = None
        state = monitor.update_state(report, None, send)
        report['flags'][0]['on'] = True
        self.assertTrue(monitor.update_state(report, state, send)['above50'])
        send.assert_not_called()

    def test_failed_delivery_keeps_previous_state(self):
        report = monitor.collect(NOW, fixtures().__getitem__)
        state = monitor.update_state(report, None)
        report['flags'][0]['on'] = True
        failed = Mock(return_value=False)
        self.assertFalse(monitor.update_state(report, state, failed)['above50'])
        delivered = Mock(return_value=True)
        self.assertTrue(monitor.update_state(report, state, delivered)['above50'])
        delivered.assert_called_once()

    def test_pushover_dry_run_and_retry(self):
        with patch.dict(os.environ, {'PUSHOVER_TOKEN': '', 'PUSHOVER_USER': ''}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(monitor.send_pushover('title', 'message'))
            self.assertIn('[dry run, no Pushover keys]\ntitle\nmessage', out.getvalue())
        reply = Mock()
        reply.__enter__ = Mock(return_value=io.StringIO('{"status":1}'))
        reply.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {'PUSHOVER_TOKEN': 'fixture', 'PUSHOVER_USER': 'fixture'}), \
                patch.object(monitor.urllib.request, 'urlopen', side_effect=[OSError('offline'), reply]) as request, \
                patch.object(monitor.time, 'sleep'), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(monitor.send_pushover('title', 'message'))
            self.assertEqual(request.call_count, 2)
            self.assertIn(b'priority=0', request.call_args.args[0].data)

    def test_main_writes_json_and_quiet_second_run(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            report = monitor.collect(NOW, fixtures().__getitem__)
            warnings, state = Path(folder) / 'warnings.json', Path(folder) / 'warning_state.json'
            with patch.object(monitor, 'WARNINGS_FILE', warnings), patch.object(monitor, 'STATE_FILE', state), \
                    patch.object(monitor, 'collect', return_value=report), patch.object(monitor, 'send_pushover') as send:
                monitor.main()
                first = json.loads(state.read_text())
                monitor.main()
                self.assertEqual(json.loads(warnings.read_text()), report)
                self.assertEqual(json.loads(state.read_text()), first)
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
