"""Real SQLite and owned protocol subprocess checks for manual timing repair."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import dataclasses
import time
import unittest
import uuid
from unittest.mock import patch

from tests import test_caption_repair as base_tests
from native.core.creator_library import CreatorLibrary
from native.core.accepted_transcript import StaleRevision
from native.services.caption_repair import RepairBlocked
from native.services.caption_timing import TimingRepairController, TimingInputError


class TimingTests(base_tests.RepairTests):
    def start(self, mode='success'):
        super().start(mode)
        self.controller = TimingRepairController(
            self.db, self.bridge, self.working, owner=5, tracks=self.tracks,
            engine_version='protocol-fixture-only',
            receipts_directory=self.root / 'repair',
        )
        return self.bg(self.controller.load, self.tid, 1)

    def unresolved(self, view, indices):
        with CreatorLibrary(self.db) as library:
            revision = view.revision
            for index in indices:
                word = library.captions.revision(self.tid, revision)['words'][index]
                updated = library.captions.edit(
                    self.tid, expected_revision=revision, operation='retime',
                    target_word_ids=[word['id']], replacements=[{
                        'text': word['text'], 'timing': {
                            'start_us': None, 'end_us': None, 'status': 'unresolved',
                        },
                    }],
                )
                revision = updated['revision']
        return self.bg(self.controller.load, self.tid, revision)

    def mutations(self):
        return [row for row in self.calls() if row['command'] == 'clipline.acceptCaptions']

    def test_timing_only_pending_recovers_same_word_and_exact_previous_pin(self):
        view = self.start()
        pending = self.unresolved(view, [2])
        self.assertTrue(pending.can_time)
        self.assertFalse(pending.can_edit)
        self.export_view('pending-timing', pending)
        old = self.accepted_now()['words'][2]
        result = self.bg(self.controller.save_timing, pending, old['id'], 950000, 1180000)
        self.assertEqual(result.state, 'saved_applied')
        self.assertEqual(result.revision, 3)
        current = self.accepted_now()['words'][2]
        for key in ('id', 'text', 'parent_word_ids', 'machine_refs', 'text_origin'):
            self.assertEqual(current[key], old[key])
        self.assertEqual(current['timing']['status'], 'manually_timed')
        self.assertEqual(current['timing_provenance'], {'kind': 'human_manual', 'at_revision': 3})
        self.assertEqual(len(self.mutations()), 1)
        self.assertEqual(self.mutations()[0]['params']['expectedAcceptedRevision'], self.pin)
        self.assertEqual(self.mutations()[0]['params']['projectionInput']['accepted']['revision'], 3)
        self.export_view('timed', result)

    def test_multiple_unresolved_words_save_pending_without_mutating_until_all_usable(self):
        pending = self.unresolved(self.start(), [0, 1])
        first = self.bg(self.controller.save_timing, pending, pending.words[0].id, 150000, 300000)
        self.assertEqual(first.state, 'saved_pending')
        self.assertTrue(first.can_time)
        self.assertFalse(first.can_apply)
        self.assertFalse(first.can_edit)
        self.assertEqual(len(self.mutations()), 0)
        self.assertFalse(self.controller.barrier.exists())
        second = self.bg(self.controller.save_timing, first, first.words[1].id, 550000, 700000)
        self.assertEqual(second.state, 'saved_applied')
        self.assertEqual(second.revision, 5)
        self.assertEqual(len(self.mutations()), 1)

    def test_initial_unaligned_words_use_no_previous_pin_and_apply_only_when_complete(self):
        with CreatorLibrary(self.db) as library:
            identity = library.captions.identity(self.tid)
            self.tid = library.captions.create(identity)
            hypothesis = {
                'run_id': str(uuid.uuid4()), 'processing_state': 'complete', **identity,
                'words': [{'id': str(uuid.uuid4()), 'text': text} for text in ('I', 'hit', 'shot')],
            }
            library.captions.propose_hypothesis(self.tid, hypothesis)
            accepted = library.captions.accept_initial(self.tid, hypothesis['run_id'])
        self.pin = None
        self.projection = None
        view = self.start()
        self.assertTrue(view.can_time)
        self.assertEqual(view.state, 'saved_pending')
        for index, interval in enumerate(((100000, 300000), (500000, 700000), (900000, 1100000))):
            view = self.bg(self.controller.save_timing, view, view.words[index].id, *interval)
            self.assertEqual(len(self.mutations()), int(index == 2))
        self.assertEqual(view.state, 'saved_applied')
        self.assertEqual(view.revision, 4)
        self.assertIsNone(self.mutations()[0]['params']['expectedAcceptedRevision'])
        self.assertEqual([w['id'] for w in self.accepted_now()['words']],
                         [w['id'] for w in accepted['words']])

    def test_timing_uses_background_database_hash_and_native_owner(self):
        import threading
        view = self.start()
        observed = []
        original_init = CreatorLibrary.__init__
        original_verify = CreatorLibrary.verify_asset
        original_execute = self.bridge.execute

        def guarded_init(library, *args, **kwargs):
            observed.append(threading.current_thread())
            return original_init(library, *args, **kwargs)

        def guarded_verify(library, *args, **kwargs):
            observed.append(threading.current_thread())
            return original_verify(library, *args, **kwargs)

        def guarded_execute(*args, **kwargs):
            observed.append(threading.current_thread())
            return original_execute(*args, **kwargs)

        with patch.object(CreatorLibrary, '__init__', guarded_init), \
                patch.object(CreatorLibrary, 'verify_asset', guarded_verify), \
                patch.object(self.bridge, 'execute', guarded_execute):
            saved = self.bg(self.controller.save_timing, view, view.words[1].id, 550000, 700000)
        self.assertEqual(saved.state, 'saved_applied')
        self.assertTrue(observed)
        self.assertTrue(all(thread is not threading.main_thread() for thread in observed))
        with self.assertRaisesRegex(RuntimeError, 'background owner'):
            self.controller.save_timing(saved, saved.words[1].id, 560000, 710000)

    def test_invalid_interval_rolls_back_before_native_mutation(self):
        view = self.start()
        for start, end in ((-1, 100), (100, 100), (200, 100), (100000, 2100000),
                           (200000, 300000), (True, 700000), (500000.0, 700000)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(TimingInputError):
                    self.bg(self.controller.save_timing, view, view.words[1].id, start, end)
                self.assertEqual(self.accepted_now()['revision'], 1)
                self.assertEqual(len(self.mutations()), 0)

    def test_stale_pin_and_stale_canonical_revision_refuse_further_commit(self):
        view = self.start()
        saved = self.bg(self.controller.save_timing, view, view.words[2].id, 950000, 1180000)
        with self.assertRaises(RepairBlocked):
            self.bg(self.controller.save_timing, view, view.words[1].id, 550000, 700000)
        with CreatorLibrary(self.db) as library:
            word = library.captions.revision(self.tid, 2)['words'][1]
            library.captions.edit(self.tid, expected_revision=2, operation='retime',
                                 target_word_ids=[word['id']], replacements=[{
                                     'text': word['text'], 'timing': {
                                         'start_us': 550000, 'end_us': 700000,
                                         'status': 'manually_timed',
                                     },
                                 }])
        with self.assertRaises(StaleRevision):
            self.bg(self.controller.save_timing, saved, saved.words[0].id, 150000, 300000)
        self.assertEqual(self.accepted_now()['revision'], 3)
        self.assertEqual(len(self.mutations()), 1)

    def test_pending_text_lineage_and_forged_widget_admission_are_rejected(self):
        view = self.start()
        with CreatorLibrary(self.db) as library:
            library.captions.edit(self.tid, expected_revision=1, operation='replace',
                                 target_word_ids=[view.words[2].id],
                                 replacements=[{'text': 'shot'}])
        pending = self.bg(self.controller.load, self.tid, 2)
        self.assertFalse(pending.can_time)
        forged = dataclasses.replace(pending, can_time=True)
        with self.assertRaises(RepairBlocked):
            self.bg(self.controller.save_timing, forged, forged.words[1].id, 550000, 700000)
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(len(self.mutations()), 0)

    def test_lost_timing_reply_retains_barrier_until_read_only_reconciliation(self):
        pending = self.unresolved(self.start('close-after'), [2])
        result = self.bg(self.controller.save_timing, pending, pending.words[2].id, 950000, 1180000)
        self.assertEqual(result.state, 'saved_uncertain')
        self.assertFalse(result.can_time)
        self.assertTrue(self.controller.barrier.exists())
        with self.assertRaises(RepairBlocked):
            self.bg(self.controller.save_timing, result, result.words[1].id, 550000, 700000)
        self.assertEqual(self.accepted_now()['revision'], 3)
        self.assertEqual(len(self.mutations()), 1)
        self.export_view('timing-uncertain', result)
        checked = self.bg(self.controller.check_editor, result)
        self.assertEqual(checked.state, 'saved_applied')
        self.assertTrue(checked.can_time)
        self.assertEqual(len(self.mutations()), 1)

    def test_qt_timing_validation_keeps_draft_then_enter_applies_one_revision(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication
        from native.ui.caption_timing import TimingRepairPanel, TimingRepairBinding
        app = QApplication.instance() or QApplication([])
        panel = TimingRepairPanel()
        panel.set_view(self.start())
        panel.table.selectRow(1)
        binding = TimingRepairBinding(panel, self.controller)
        panel.show()
        app.processEvents()
        panel.start_time.setText('1e3')
        self.assertFalse(panel.save_timing.isEnabled())
        QTest.keyClick(panel.start_time, Qt.Key.Key_Return)
        self.assertFalse(panel.busy)
        self.assertEqual(self.accepted_now()['revision'], 1)
        self.assertEqual(len(self.mutations()), 0)
        ticks = []
        timer = QTimer()
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            for start, end in (('0.200000', '0.300000'), ('0.550000', '0.700000')):
                panel.start_time.setText(start)
                panel.end_time.setText(end)
                self.assertTrue(panel.save_timing.isEnabled())
                QTest.keyClick(panel.end_time, Qt.Key.Key_Return)
                self.assertTrue(panel.busy)
                deadline = time.monotonic() + 10
                while panel.busy or binding.runner._jobs:
                    app.processEvents()
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.001)
                if start == '0.200000':
                    self.assertIsNotNone(panel._timing_validation)
                    self.assertEqual(panel.status.text(), 'Timing change not saved')
                    self.assertFalse(panel._error)
                    self.assertTrue(panel.start_time.isEnabled())
                    self.assertEqual(panel.start_time.text(), start)
                    self.assertEqual(self.accepted_now()['revision'], 1)
            self.assertTrue(ticks)
            self.assertEqual(panel.view.state, 'saved_applied')
            self.assertEqual(panel.table.currentRow(), 1)
            self.assertEqual(self.accepted_now()['revision'], 2)
            self.assertEqual(len(self.mutations()), 1)
        finally:
            timer.stop()
            panel.close()
            app.processEvents()

    def test_qt_initial_background_load_updates_widgets_only_on_main_thread(self):
        import threading
        from PySide6.QtWidgets import QApplication
        from native.ui.caption_timing import TimingRepairPanel, TimingRepairBinding

        app = QApplication.instance() or QApplication([])
        observed = []

        class ObservedPanel(TimingRepairPanel):
            def set_view(self, view):
                on_main = threading.current_thread() is threading.main_thread()
                observed.append(('view', on_main))
                # A failing regression must not let a Qt paint crash the test process.
                if on_main:
                    super().set_view(view)

            def set_busy(self, busy):
                on_main = threading.current_thread() is threading.main_thread()
                observed.append(('busy', on_main))
                if on_main:
                    super().set_busy(busy)

        self.start()
        panel = ObservedPanel()
        binding = TimingRepairBinding(panel, self.controller)
        panel.show()
        app.processEvents()
        try:
            binding.load(self.tid, 1)
            deadline = time.monotonic() + 10
            while binding.runner._jobs or not any(kind == 'view' for kind, _ in observed):
                app.processEvents()
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            self.assertTrue(all(on_main for _, on_main in observed), observed)
            self.assertEqual(panel.view.revision, 1)
            self.assertEqual(panel.view.state, 'saved_applied')
            self.assertFalse(panel.busy)
        finally:
            panel.close()
            app.processEvents()

    def test_source_clock_is_exact_and_signal_does_not_truncate_long_stream_times(self):
        from PySide6.QtWidgets import QApplication
        from native.ui.caption_timing import TimingRepairPanel, parse_source_seconds, source_seconds
        self.assertEqual(parse_source_seconds('3000.000001'), 3000000001)
        self.assertEqual(source_seconds(3000000001), '3000.000001')
        for text in ('1e3', 'NaN', 'inf', '-1', '0.0000001'):
            with self.assertRaises(ValueError):
                parse_source_seconds(text)
        app = QApplication.instance() or QApplication([])
        panel = TimingRepairPanel()
        panel.set_view(dataclasses.replace(self.start(), source_end_us=6000000000))
        panel.table.selectRow(0)
        captured = []
        panel.timing_requested.connect(lambda *args: captured.append(args))
        panel.start_time.setText('3000.000001')
        panel.end_time.setText('3000.000002')
        panel.save_timing.click()
        self.assertEqual(captured[0][2:], (3000000001, 3000000002))
        panel.close()
        app.processEvents()

    def test_panel_keeps_only_one_edit_group_active_and_explains_mixed_draft_recovery(self):
        from PySide6.QtWidgets import QApplication
        from native.ui.caption_timing import TimingRepairPanel
        app = QApplication.instance() or QApplication([])
        panel = TimingRepairPanel()
        view = self.start()
        panel.set_view(view)
        panel.table.selectRow(1)
        panel.spelling.setText('landed')
        self.assertTrue(panel.save.isEnabled())
        self.assertFalse(panel.start_time.isEnabled())
        self.assertFalse(panel.end_time.isEnabled())
        panel.spelling.setText('hit')
        panel.start_time.setText('0.550000')
        self.assertTrue(panel.save_timing.isEnabled())
        self.assertFalse(panel.spelling.isEnabled())
        # A supplied mixed draft still gives a working recovery action.
        panel.spelling.setText('landed')
        self.assertFalse(panel.save.isEnabled())
        self.assertFalse(panel.save_timing.isEnabled())
        self.assertTrue(panel.reload.isEnabled())
        self.assertIn('Reload saved words', panel.message.text())
        self.assertEqual(self.accepted_now()['revision'], 1)
        self.assertEqual(len(self.mutations()), 0)
        panel.close()
        app.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
