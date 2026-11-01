import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from openarm_runtime.report import QueuedReport, Report


class QueuedReportTests(unittest.TestCase):
    def test_slow_disk_does_not_block_producer_and_flush_preserves_order(self):
        entered, release = threading.Event(), threading.Event()
        original = Report.record_at
        def slow(report, event, elapsed):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test writer was not released')
            original(report, event, elapsed)
        with tempfile.TemporaryDirectory() as root, patch.object(Report, 'record_at', slow):
            path = Path(root)/'report.json'
            report = QueuedReport(path, {})
            try:
                report.record({'event': 'first'})
                self.assertTrue(entered.wait(1))
                sample = {'event': 'sample', 'values': [1]}
                report.record(sample)  # Must complete while disk writer is blocked.
                sample['values'][0] = 99
                self.assertEqual(report.queue.qsize(), 1)
            finally:
                release.set()
                report.close()
            events = json.loads(path.read_text())['events']
            self.assertEqual([e['event'] for e in events], ['first', 'sample'])
            self.assertEqual(events[1]['values'], [1])
            self.assertLessEqual(events[0]['elapsed_s'], events[1]['elapsed_s'])

    def test_writer_failure_is_reported_in_control_check_and_close(self):
        with tempfile.TemporaryDirectory() as root, patch.object(Report, 'record_at', side_effect=OSError('disk full')):
            report = QueuedReport(Path(root)/'report.json', {})
            report.record({'event': 'sample'})
            report.worker.join(1)
            self.assertFalse(report.worker.is_alive())
            for call in (report.check, report.close):
                with self.assertRaisesRegex(RuntimeError, 'disk full'):
                    call()

    def test_queue_is_bounded_and_cannot_silently_drop_events(self):
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
        with tempfile.TemporaryDirectory() as root, patch.object(Report, 'record_at', slow):
            report = QueuedReport(Path(root)/'report.json', {})
            try:
                report.record({'event': 'first'})
                self.assertTrue(entered.wait(1))
                for _ in range(128):
                    report.record({'event': 'queued'})
                with self.assertRaisesRegex(RuntimeError, 'queue full'):
                    report.record({'event': 'overflow'})
            finally:
                release.set()
                report.close()
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                report.record({'event': 'late'})
