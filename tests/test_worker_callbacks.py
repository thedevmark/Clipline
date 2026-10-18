"""Actual QThread callbacks with lambdas, overridden slots and panel loading."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import threading
import time
import unittest

from PySide6.QtCore import QObject, QThread, Slot, QTimer
from PySide6.QtWidgets import QApplication
from native import workers

APP = None
OBSERVED = []


def thread_record(label):
    item = {'label': label,
            'python_main': threading.current_thread() is threading.main_thread(),
            'qt_main': QThread.currentThread() == APP.thread()}
    OBSERVED.append(item)
    return item


class BaseReceiver(QObject):
    @Slot(object)
    def finished(self, value):
        raise AssertionError('The override must receive the result')


class DerivedReceiver(BaseReceiver):
    def __init__(self):
        super().__init__()
        self.received = []

    @Slot(object)
    def finished(self, value):
        self.received.append((thread_record('overridden-finished'), value))


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global APP
        APP = QApplication.instance() or QApplication([])

    def drain(self, runner):
        deadline = time.monotonic() + 5
        while runner._jobs:
            APP.processEvents()
            self.assertLess(time.monotonic(), deadline, 'Owned worker did not settle')
            time.sleep(.001)
        self.assertFalse(runner._callbacks)

    def test_lambda_success_progress_percentage_are_on_application_thread(self):
        runner = workers.JobRunner()
        seen = []

        def work(job):
            self.assertFalse(thread_record('work')['qt_main'])
            job.progress.emit('step')
            job.progress_pct.emit(.75)
            return {'value': 7}

        def receive(label, value):
            record = thread_record(label)
            self.assertTrue(record['python_main'] and record['qt_main'])
            seen.append((label, value))

        runner.run(work, on_progress=lambda value: receive('progress', value),
                   on_progress_pct=lambda value: receive('percentage', value),
                   on_finished=lambda value: receive('finished', value))
        self.drain(runner)
        self.assertEqual(seen, [('progress', 'step'), ('percentage', .75), ('finished', {'value': 7})])

    def test_inherited_overridden_qobject_slot_runs_on_application_thread(self):
        receiver = DerivedReceiver()
        runner = workers.JobRunner()
        runner.run(lambda job: 'result', on_finished=receiver.finished)
        self.drain(runner)
        self.assertEqual(receiver.received[0][1], 'result')
        self.assertTrue(receiver.received[0][0]['python_main'])
        self.assertTrue(receiver.received[0][0]['qt_main'])

    def test_error_and_prior_progress_are_queued_once_without_success(self):
        runner = workers.JobRunner()
        seen = []

        def fail(job):
            job.progress.emit('before error')
            raise ValueError('authored failure')

        def receive(kind, value):
            self.assertTrue(thread_record(kind)['qt_main'])
            seen.append((kind, value))

        runner.run(fail, on_progress=lambda value: receive('progress', value),
                   on_error=lambda value: receive('error', value),
                   on_finished=lambda value: receive('unexpected success', value))
        self.drain(runner)
        self.assertEqual(seen, [('progress', 'before error'), ('error', 'ValueError: authored failure')])

    def test_relay_and_pair_stay_alive_after_thread_exit_until_terminal_delivery(self):
        runner = workers.JobRunner()
        delivered = []
        runner.run(lambda job: 12, on_finished=lambda result: delivered.append(result))
        thread, job = runner._jobs[0]
        self.assertTrue(thread.wait(2000), 'Thread exit must not require GUI event processing')
        self.assertEqual(runner._jobs, [(thread, job)])
        self.assertIn(thread, runner._callbacks)
        self.assertFalse(delivered)
        self.drain(runner)
        self.assertEqual(delivered, [12])

    def test_terminal_callback_can_quit_event_loop_then_wait_all_retires_pair(self):
        runner = workers.JobRunner()
        received = []

        def finished(value):
            self.assertTrue(thread_record('quit-terminal')['qt_main'])
            self.assertTrue(runner._jobs)
            self.assertTrue(runner._callbacks)
            received.append(value)
            APP.quit()

        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(APP.quit)
        timeout.start(3000)
        runner.run(lambda job: 'quit', on_finished=finished)
        APP.exec()
        timeout.stop()
        self.assertEqual(received, ['quit'])
        started = time.monotonic()
        runner.wait_all(2000)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertFalse(runner._jobs)
        self.assertFalse(runner._callbacks)

    def test_wait_all_timeout_keeps_live_pair_then_allows_normal_delivery(self):
        gate = threading.Event()
        runner = workers.JobRunner()
        received = []
        runner.run(lambda job: gate.wait(3), on_finished=received.append)
        try:
            runner.wait_all(1)
            self.assertTrue(runner._jobs)
            self.assertTrue(runner._callbacks)
            self.assertFalse(received)
        finally:
            gate.set()
            self.drain(runner)
        self.assertEqual(received, [True])

    def test_background_caller_is_rejected_before_start(self):
        runner = workers.JobRunner()
        failures = []

        def caller():
            try:
                runner.run(lambda job: 'invalid caller')
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=caller)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        self.assertFalse(runner._jobs)
        self.assertFalse(runner._callbacks)


if __name__ == '__main__':
    unittest.main(verbosity=2)
