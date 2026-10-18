import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import concurrent.futures
import dataclasses
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).parent
from native.core.creator_library import CreatorLibrary
from native.core.accepted_transcript import StaleRevision
from native.services.native_checkpoint import WorkingCopy
from native.services.filmcraft_bridge import FilmcraftBridge
from native.services.caption_worker.process_tree import ProcessTree
from native.services import caption_repair as c

class RepairTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipline-caption-repair-')
        self.root = Path(self.temp.name)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.peer = None
        self.scope = None
        self.streams = []
        self.db = self.root / 'library.sqlite'
        self.source = self.root / 'Authored-caption-fixture.bin'
        self.original = b'original fixture media bytes'
        self.source.write_bytes(self.original)
        with CreatorLibrary(self.db) as library:
            asset = library.register_asset(self.source)
            self.asset = asset
            identity = {
                'source': {
                    'asset_id': asset.id,
                    'sha256': asset.sha256,
                    'path': asset.path,
                    'audio_stream_index': 1,
                    'channels': [0, 1],
                    'mix': 'unknown',
                },
                'audio': {
                    'path': 'authored.wav',
                    'pcm_sha256': 'a' * 64,
                    'wav_sha256': 'b' * 64,
                    'sample_rate': 16000,
                    'sample_count': 32000,
                    'channels': 1,
                    'sample_width': 2,
                    'sample_zero_source_us': 0,
                    'extraction': {
                        'source_sha256': asset.sha256,
                        'argv': ['fixture'],
                        'tool_version': 'authored fixture',
                    },
                },
                'range': {'start_us': 0, 'end_us': 2000000},
            }
            self.tid = library.captions.create(identity)
            hypothesis = {
                'run_id': str(uuid.uuid4()),
                'processing_state': 'complete',
                **identity,
                'words': [{'id': str(uuid.uuid4()), 'text': text} for text in ('I', 'hit', 'shott')],
            }
            alignment = {
                'run_id': str(uuid.uuid4()),
                'hypothesis_run_id': hypothesis['run_id'],
                'target_transcript_revision': None,
                'words': [{'word_id': word['id'], 'text': word['text'], 'timing': {'start_us': 100000 + i * 400000, 'end_us': 350000 + i * 400000, 'status': 'aligned'}} for i, word in enumerate(hypothesis['words'])],
            }
            library.captions.propose_hypothesis(self.tid, hypothesis)
            library.captions.propose_alignment(self.tid, hypothesis['run_id'], alignment)
            self.accepted = library.captions.accept_initial(
                self.tid,
                hypothesis['run_id'],
                alignment_id=alignment['run_id'],
            )
            self.projection = library.captions.projection_input(self.tid, revision=1)
        self.path = self.root / 'working.fcproj'
        self.path.write_bytes(b'opaque owned working bytes')
        self.working = WorkingCopy(self.path, str(uuid.uuid4()), 1)
        self.pin = {'transcript_id': self.tid, 'revision': 1, 'payload_sha256': 'c' * 64}
        self.tracks = [{'sequence': seq, 'track': track, 'rules': {'maxChars': 32, 'lines': 2, 'minDurationUs': 100000, 'maxDurationUs': 3000000, 'gapFrames': 0, 'breakPauseUs': 400000}, 'defaultSettings': ''} for seq, track in ((5, 8), (13, 16), (21, 24))]

    def tearDown(self):
        if self.scope:
            proof = self.scope.stop()
            self.scope.close()
            self.peer.wait(timeout=5)
            self.assertEqual(proof['disposition'], 'stopped')
            (self.root / 'peer-stop.json').write_text(json.dumps(proof))
        for stream in self.streams:
            stream.close()
        self.pool.shutdown(wait=True)
        self.temp.cleanup()

    def start(self, mode='success'):
        config = {
            'mode': mode,
            'source': str(self.source),
            'bytes': self.asset.bytes,
            'working': str(self.path),
            'pin': self.pin,
            'projection': self.projection,
        }
        config_path = self.root / 'peer.json'
        config_path.write_text(json.dumps(config))
        out = (self.root / 'peer.stdout').open('wb')
        err = (self.root / 'peer.stderr').open('wb')
        self.streams = [out, err]
        self.peer = subprocess.Popen(
            [sys.executable, str(ROOT / 'fixtures' / 'caption_repair_peer.py'), str(config_path)],
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.scope = ProcessTree(self.peer)
        self.peer.stdin.write(b'start\n')
        self.peer.stdin.flush()
        self.peer.stdin.close()
        deadline = time.monotonic() + 3
        while not (self.root / 'ready.json').exists():
            self.assertIsNone(self.peer.poll())
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        port = json.loads((self.root / 'ready.json').read_text())['port']
        self.bridge = FilmcraftBridge(port, timeout=2)
        self.controller = c.CaptionRepairController(
            self.db,
            self.bridge,
            self.working,
            owner=5,
            tracks=self.tracks,
            engine_version='protocol-fixture-only',
            receipts_directory=self.root / 'repair',
        )
        return self.bg(self.controller.load, self.tid, 1)

    def bg(self, fn, *args):
        return self.pool.submit(fn, *args).result(timeout=15)

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def accepted_now(self):
        with CreatorLibrary(self.db) as library:
            return library.captions.revision(self.tid)

    def export_view(self, name, view):
        (self.root / (name + '-view.json')).write_text(
            json.dumps(dataclasses.asdict(view), indent=2),
        )

    def test_one_action_persists_correction_lineage_then_applies_exact_revision_once(self):
        view = self.start()
        self.assertEqual(view.state, 'saved_applied')
        self.export_view('initial', view)
        old = view.words[2]
        result = self.bg(self.controller.save_spelling, view, old.id, 'shot')
        self.assertEqual(result.state, 'saved_applied')
        self.assertEqual(result.revision, 2)
        accepted = self.accepted_now()
        word = accepted['words'][2]
        self.assertEqual(word['text'], 'shot')
        self.assertNotEqual(word['id'], old.id)
        self.assertEqual(word['parent_word_ids'], [old.id])
        self.assertEqual(word['timing']['status'], 'inherited')
        self.assertEqual(word['timing']['start_us'], old.start_us)
        self.assertEqual(word['timing']['end_us'], old.end_us)
        self.assertEqual(word['timing_provenance']['from_revision'], 1)
        commands = [r for r in self.calls() if r['command'] == 'clipline.acceptCaptions']
        self.assertEqual(len(commands), 1)
        params = commands[0]['params']
        self.assertEqual(params['projectionInput']['accepted'], accepted)
        self.assertEqual(params['expectedAcceptedRevision'], self.pin)
        self.assertEqual(params['expectedProjectPath'], str(self.path))
        self.assertEqual(self.path.read_bytes(), b'opaque owned working bytes')
        self.assertFalse(self.controller.barrier.exists())
        self.export_view('applied', result)
        self.assertTrue((Path(result.receipt_directory) / 'saved.json').exists())
        self.assertTrue((Path(result.receipt_directory) / 'application.json').exists())

    def test_stale_correction_does_not_advance_store_or_send_native_mutation(self):
        view = self.start()
        self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        with self.assertRaises(StaleRevision):
            self.bg(self.controller.save_spelling, view, view.words[1].id, 'landed')
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 1)

    def test_known_source_failure_stays_saved_pending_and_explicit_apply_does_not_follow_head(self):
        view = self.start()
        self.source.write_bytes(b'changed')
        pending = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(pending.state, 'saved_pending')
        self.assertIn('source file changed', pending.message)
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 0)
        self.assertFalse(self.controller.barrier.exists())
        self.assertFalse(pending.can_edit)
        self.export_view('pending', pending)
        with self.assertRaises(c.RepairBlocked):
            self.bg(self.controller.save_spelling, pending, pending.words[1].id, 'landed')
        self.assertEqual(self.accepted_now()['revision'], 2)
        with CreatorLibrary(self.db) as library:
            library.captions.edit(
                self.tid,
                expected_revision=2,
                operation='replace',
                target_word_ids=[pending.words[1].id],
                replacements=[{'text': 'landed'}],
            )
        self.source.write_bytes(self.original)
        applied = self.bg(self.controller.apply_saved, pending)
        self.assertEqual(applied.revision, 2)
        self.assertEqual(self.accepted_now()['revision'], 3)
        command = [r for r in self.calls() if r['command'] == 'clipline.acceptCaptions'][0]
        self.assertEqual(command['params']['projectionInput']['accepted']['revision'], 2)
        self.assertEqual(
            command['params']['projectionInput']['accepted']['words'][1]['text'],
            'hit',
        )

    def test_lost_native_reply_never_retries_and_read_only_matching_projection_reconciles(self):
        view = self.start('close-after')
        uncertain = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(uncertain.state, 'saved_uncertain')
        self.assertFalse(uncertain.can_edit)
        self.assertTrue(self.controller.barrier.exists())
        self.assertEqual(self.accepted_now()['words'][2]['text'], 'shot')
        self.export_view('uncertain', uncertain)
        with self.assertRaises(c.RepairBlocked):
            self.bg(self.controller.apply_saved, uncertain)
        with self.assertRaises(c.RepairBlocked):
            self.bg(self.controller.save_spelling, uncertain, uncertain.words[1].id, 'landed')
        with CreatorLibrary(self.db) as library:
            library.captions.edit(
                self.tid,
                expected_revision=2,
                operation='replace',
                target_word_ids=[uncertain.words[1].id],
                replacements=[{'text': 'landed'}],
            )
        reloaded = self.bg(self.controller.reload_saved, self.tid)
        self.assertEqual(reloaded.revision, 2)
        self.assertEqual(self.accepted_now()['revision'], 3)
        observed = self.bg(self.controller.check_editor, reloaded)
        self.assertEqual(observed.state, 'saved_applied')
        self.assertFalse(self.controller.barrier.exists())
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 1)

    def test_old_pin_observation_does_not_clear_uncertainty_or_allow_retry_after_new_controller(self):
        view = self.start('drop-before')
        uncertain = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        newer = c.CaptionRepairController(
            self.db,
            self.bridge,
            self.working,
            owner=5,
            tracks=self.tracks,
            engine_version='protocol-fixture-only',
            receipts_directory=self.root / 'repair',
        )
        loaded = self.bg(newer.load, self.tid, 2)
        self.assertEqual(loaded.state, 'saved_uncertain')
        checked = self.bg(newer.check_editor, loaded)
        self.assertEqual(checked.state, 'saved_uncertain')
        self.assertTrue(newer.barrier.exists())
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 1)

    def test_competing_observer_cannot_retire_a_newer_application_barrier(self):
        view = self.start('close-after')
        uncertain = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        other = c.CaptionRepairController(
            self.db, self.bridge, self.working, owner=5, tracks=self.tracks,
            engine_version='protocol-fixture-only', receipts_directory=self.root / 'repair',
        )
        observed = threading.Event()
        release = threading.Event()
        write = c.write_once

        def pause_observation(path, value):
            if (path.name.startswith('observed-')
                    and threading.current_thread().name.startswith('caption-observer')):
                observed.set()
                if not release.wait(5):
                    raise RuntimeError('The competing observer did not release its operation')
            return write(path, value)

        with patch.object(c, 'write_once', pause_observation):
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='caption-observer',
            ) as pool:
                delayed = pool.submit(other.check_editor, uncertain)
                try:
                    self.assertTrue(observed.wait(5), 'The native observation did not complete')
                    with self.assertRaises(c.RepairBusy):
                        self.bg(self.controller.check_editor, uncertain)
                    with self.assertRaises(c.RepairBusy):
                        self.bg(self.controller.load, self.tid, 2)
                    self.assertEqual(json.loads(self.controller.barrier.read_text())['revision'], 2)
                finally:
                    release.set()
                confirmed = delayed.result(timeout=5)

        self.assertEqual(confirmed.state, 'saved_applied')
        newer = self.bg(self.controller.save_spelling, confirmed, confirmed.words[1].id, 'landed')
        self.assertEqual(newer.state, 'saved_uncertain')
        self.assertEqual(json.loads(self.controller.barrier.read_text())['revision'], 3)
        loaded = self.bg(other.load, self.tid, 3)
        self.assertEqual(loaded.state, 'saved_uncertain')
        self.assertFalse(loaded.can_apply)
        self.assertEqual(self.accepted_now()['revision'], 3)

    def test_native_command_error_is_uncertain_and_canonical_correction_survives(self):
        view = self.start('reject')
        result = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(result.state, 'saved_uncertain')
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 1)

    def test_pending_unresolved_timing_refuses_another_correction_without_fabrication(self):
        view = self.start()
        with CreatorLibrary(self.db) as library:
            accepted = library.captions.edit(
                self.tid,
                expected_revision=1,
                operation='retime',
                target_word_ids=[view.words[2].id],
                replacements=[
                    {
                        'text': 'shott',
                        'timing': {'start_us': None, 'end_us': None, 'status': 'unresolved'},
                    },
                ],
            )
        loaded = self.bg(self.controller.load, self.tid, 2)
        self.assertEqual(loaded.state, 'saved_pending')
        self.assertFalse(loaded.can_apply)
        self.assertFalse(loaded.can_edit)
        with self.assertRaises(c.RepairBlocked):
            self.bg(self.controller.save_spelling, loaded, loaded.words[2].id, 'shot')
        word = self.accepted_now()['words'][2]
        self.assertEqual(word['timing']['status'], 'unresolved')
        self.assertIsNone(word['timing']['start_us'])
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(sum((r['command'] == 'clipline.acceptCaptions' for r in self.calls())), 0)
        self.export_view('timing-review', loaded)

    def test_database_hash_and_native_calls_run_only_on_background_owner(self):
        view = self.start()
        threads = []
        init = CreatorLibrary.__init__
        verify = CreatorLibrary.verify_asset
        execute = self.bridge.execute

        def guarded_init(lib, *args, **kw):
            threads.append(threading.current_thread())
            return init(lib, *args, **kw)

        def guarded_verify(lib, *args, **kw):
            threads.append(threading.current_thread())
            return verify(lib, *args, **kw)

        def guarded_execute(*args, **kw):
            threads.append(threading.current_thread())
            return execute(*args, **kw)
        with patch.object(CreatorLibrary, '__init__', guarded_init), patch.object(CreatorLibrary, 'verify_asset', guarded_verify), patch.object(self.bridge, 'execute', guarded_execute):
            result = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(result.state, 'saved_applied')
        self.assertTrue(threads)
        self.assertTrue(all((t is not threading.main_thread() for t in threads)))
        with self.assertRaisesRegex(RuntimeError, 'background owner'):
            self.controller.load(self.tid, 2)

    def test_second_correction_uses_returned_native_pin_and_only_one_more_application(self):
        view = self.start()
        first = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        second = self.bg(self.controller.save_spelling, first, first.words[1].id, 'landed')
        self.assertEqual(second.state, 'saved_applied')
        self.assertEqual(second.revision, 3)
        calls = [row for row in self.calls() if row['command'] == 'clipline.acceptCaptions']
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['params']['expectedAcceptedRevision'], first.native_pin)
        self.assertEqual([w['text'] for w in self.accepted_now()['words']], ['I', 'landed', 'shot'])

    def test_application_receipt_failure_is_uncertain_after_canonical_save_and_native_edit(self):
        view = self.start()
        write = c.write_once

        def fail_result(path, value):
            if path.name == 'application.json':
                raise OSError('fixture result receipt unavailable')
            return write(path, value)
        with patch.object(c, 'write_once', fail_result):
            result = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(result.state, 'saved_uncertain')
        self.assertTrue(self.controller.barrier.exists())
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(
            sum((row['command'] == 'clipline.acceptCaptions' for row in self.calls())),
            1,
        )
        confirmed = self.bg(self.controller.check_editor, result)
        self.assertEqual(confirmed.state, 'saved_applied')
        self.assertFalse(self.controller.barrier.exists())

    def test_application_intent_io_failure_returns_saved_pending_without_native_mutation(self):
        view = self.start()
        write = c.write_once

        def fail_intent(path, value):
            if path.name == 'apply-intent.json':
                raise OSError('fixture application intent unavailable')
            return write(path, value)
        with patch.object(c, 'write_once', fail_intent):
            result = self.bg(self.controller.save_spelling, view, view.words[2].id, 'shot')
        self.assertEqual(result.state, 'saved_pending')
        self.assertFalse(result.can_apply)
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertEqual(
            sum((row['command'] == 'clipline.acceptCaptions' for row in self.calls())),
            0,
        )

    def test_service_enforces_editable_applied_state_independently_of_widget(self):
        view = self.start()
        for supplied in (dataclasses.replace(view, can_edit=False), dataclasses.replace(view, state='saved_pending', can_edit=True)):
            with self.subTest(state=supplied.state, can_edit=supplied.can_edit):
                with self.assertRaises(c.RepairBlocked):
                    self.bg(self.controller.save_spelling, supplied, supplied.words[2].id, 'shot')
        self.assertEqual(self.accepted_now()['revision'], 1)
        self.assertEqual(
            sum((row['command'] == 'clipline.acceptCaptions' for row in self.calls())),
            0,
        )

    def test_qt_one_keyboard_action_uses_worker_and_keeps_selected_word(self):
        view = self.start()
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtTest import QTest
        from native.ui.caption_repair import CaptionRepairPanel, CaptionRepairBinding
        app = QApplication.instance() or QApplication([])
        panel = CaptionRepairPanel()
        panel.set_view(view)
        panel.table.selectRow(2)
        binding = CaptionRepairBinding(panel, self.controller)
        panel.show()
        app.processEvents()
        self.assertFalse(panel.save.isEnabled())
        panel.spelling.setText('two words')
        self.assertFalse(panel.save.isEnabled())
        panel.spelling.setText('shot')
        self.assertTrue(panel.save.isEnabled())
        panel.spelling.setFocus()
        ticks = []
        timer = QTimer()
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        QTest.keyClick(panel.spelling, Qt.Key.Key_Return)
        self.assertTrue(panel.busy)
        self.assertFalse(panel.save.isEnabled())
        deadline = time.monotonic() + 10
        while panel.busy or binding.runner._jobs:
            app.processEvents()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.001)
        timer.stop()
        self.assertTrue(ticks)
        self.assertEqual(panel.view.state, 'saved_applied')
        self.assertEqual(panel.selected_word().text, 'shot')
        self.assertEqual(panel.table.currentRow(), 2)
        self.assertEqual(self.accepted_now()['revision'], 2)
        self.assertFalse(panel.apply.isVisible())
        panel.close()
        app.processEvents()
if __name__ == '__main__':
    unittest.main(verbosity=2)
