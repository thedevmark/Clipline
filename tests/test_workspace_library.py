"""Persistent library identity and real Qt background/navigation boundaries."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from native.core.creator_library import CreatorLibrary, FingerprintMismatch, LibraryError
from native.core.twitch_inbox import TwitchClip, TwitchClipPage
from native.services.workspace_library import WorkspaceLibrary


def clip(identity, title='Authored clip metadata'):
    return TwitchClip(identity, '42', '43', '44', title, '2026-10-18T12:00:00Z',
                      1_234_567, 10, 'https://clips.twitch.tv/' + identity, None, None)


class WorkspaceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clipline-workspace-')
        self.root = Path(self.temp.name)
        self.service = WorkspaceLibrary(self.root)
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.source = self.root / 'Authored source.mp4'
        self.source.write_bytes(b'not decoded; file identity fixture')
        self.template = self.root / 'Authored template.fcproj'
        self.template.write_bytes(b'not a qualified native template; identity fixture')
        with CreatorLibrary(self.service.database) as library:
            self.asset = library.register_asset(self.source)
            self.template_asset = library.register_asset(self.template)
            self.descriptor = {'name': 'Original Kit', 'asset_refs': {'native_template': self.template_asset.id},
                               'properties': {}, 'layout_profiles': [], 'vocabulary': ['deutschmark']}
            self.kit = library.create_kit(self.descriptor)
            self.first = library.create_moment(title='First moment', source_asset_id=self.asset.id,
                source_in_us=100_000, source_out_us=1_200_000, kit_id=self.kit, kit_version=1)
            library.revise_kit(self.kit, {**self.descriptor, 'name': 'New Kit'}, expected_version=1)
            self.second = library.create_moment(title='Second moment', source_asset_id=self.asset.id,
                source_in_us=2_000_000, source_out_us=4_000_000, kit_id=self.kit, kit_version=2)
            library.record_twitch_clips([clip('One'), clip('Two')])

    def tearDown(self):
        self.pool.shutdown(wait=True)
        self.temp.cleanup()

    def bg(self, fn, *args, **kwargs):
        return self.pool.submit(fn, *args, **kwargs).result(timeout=10)


class WorkspaceTests(WorkspaceFixture):
    def test_service_rejects_main_thread_before_database_or_auth_work(self):
        for fn, args in ((self.service.page, ('assets',)), (self.service.register_file, (self.source,)),
                         (self.service.verify_file, (self.asset.id,)),
                         (self.service.kit_details, (self.kit, 1)),
                         (self.service.connect_twitch, (None, threading.Event())),
                         (self.service.refresh_clips, ('bad', 'bad', threading.Event()))):
            with self.subTest(function=fn.__name__), self.assertRaisesRegex(RuntimeError, 'background owner'):
                fn(*args)

    def test_restart_reuses_source_and_exact_pinned_kit_without_file_imports(self):
        with patch('native.core.creator_library._fingerprint', side_effect=AssertionError('No media IO on page load')):
            first = self.bg(self.service.page, 'moments', limit=1)
            restarted = WorkspaceLibrary(self.root)
            second = self.bg(restarted.page, 'moments', first.next_cursor, limit=1)
            assets = self.bg(restarted.page, 'assets')
        self.assertEqual(first.entries[0].id, self.first.id)
        self.assertIn('Original Kit · v1', first.entries[0].columns)
        self.assertEqual(second.entries[0].id, self.second.id)
        self.assertIn('New Kit · v2', second.entries[0].columns)
        self.assertIsNone(second.next_cursor)
        self.assertEqual(len(assets.entries), 2)
        self.assertEqual(self.source.read_bytes(), b'not decoded; file identity fixture')

    def test_kit_paging_and_immutable_version_selection_do_not_follow_latest(self):
        with CreatorLibrary(self.service.database) as library:
            other = library.create_kit({**self.descriptor, 'name': 'Other Kit'})
            first = library.list_kits(limit=1)
            second = library.list_kits(after=first['next_cursor'], limit=1)
            self.assertEqual(first['items'][0].id, self.kit)
            self.assertEqual(first['items'][0].version, 2)
            self.assertEqual(second['items'][0].id, other)
            self.assertIsNone(second['next_cursor'])
            for kwargs in ({'after': True}, {'after': -1}, {'limit': 0}, {'limit': 201}):
                with self.assertRaises(LibraryError):
                    library.list_kits(**kwargs)
                with self.assertRaises(LibraryError):
                    library.list_kit_versions(self.kit, **kwargs)
            with self.assertRaisesRegex(LibraryError, 'Unknown Kit'):
                library.list_kit_versions('missing')
        old = self.bg(self.service.page, 'versions', context=self.kit, limit=1)
        self.assertEqual(old.entries[0].revision, 1)
        with CreatorLibrary(self.service.database) as library:
            library.revise_kit(self.kit, {**self.descriptor, 'name': 'Newest'}, expected_version=2)
        detail = self.bg(self.service.kit_details, old.entries[0].id, old.entries[0].revision)
        self.assertIn('Original Kit\nVersion: 1', detail)
        self.assertNotIn('Newest', detail)
        remaining = self.bg(self.service.page, 'versions', old.next_cursor, context=self.kit)
        self.assertEqual([entry.revision for entry in remaining.entries], [2, 3])

    def test_file_registration_deduplicates_and_changed_file_cannot_be_verified(self):
        duplicate = self.root / 'duplicate.mp4'
        duplicate.write_bytes(self.source.read_bytes())
        again = self.bg(self.service.register_file, duplicate)
        self.assertEqual(again.id, self.asset.id)
        self.assertIn(str(self.source), again.details)
        self.source.write_bytes(b'changed on disk')
        with self.assertRaises(FingerprintMismatch):
            self.bg(self.service.verify_file, self.asset.id)
        with CreatorLibrary(self.service.database) as library:
            self.assertEqual(library.asset(self.asset.id), self.asset)
            self.assertEqual(len(library.list_assets()['items']), 2)

    def test_refresh_consumes_pages_and_saves_metadata_without_importing_media(self):
        pages = [TwitchClipPage((clip('Two', 'Updated'),), 'next'), TwitchClipPage((clip('Three'),), None)]
        with patch('native.services.twitch_api.current_user', return_value={'id': '42', 'display_name': 'Creator'}), \
                patch('native.services.twitch_api.clips_page', side_effect=pages) as fetch, \
                patch('native.core.creator_library._fingerprint', side_effect=AssertionError('Metadata only')):
            result = self.bg(self.service.refresh_clips, '2026-10-17T00:00:00Z',
                             '2026-10-19T00:00:00Z', threading.Event())
        self.assertEqual(result.received, 2)
        self.assertFalse(result.cancelled)
        self.assertEqual([call.kwargs['after'] for call in fetch.call_args_list], [None, 'next'])
        rows = self.bg(self.service.page, 'clips').entries
        self.assertEqual(len(rows), 3)
        self.assertEqual(next(row for row in rows if row.id == 'Two').columns[0], 'Updated')
        self.assertEqual(len(self.bg(self.service.page, 'assets').entries), 2)

    def test_cancel_and_late_failure_preserve_completed_clip_pages(self):
        cancel = threading.Event()

        def page(*args, **kwargs):
            cancel.set()
            return TwitchClipPage((clip('CancelledPage'),), 'later')

        with patch('native.services.twitch_api.current_user', return_value={'id': '42', 'display_name': 'Creator'}), \
                patch('native.services.twitch_api.clips_page', side_effect=page) as fetch:
            result = self.bg(self.service.refresh_clips, '2026-10-17T00:00:00Z', '2026-10-19T00:00:00Z', cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(fetch.call_count, 1)
        self.assertIn('CancelledPage', [row.id for row in self.bg(self.service.page, 'clips').entries])
        with patch('native.services.twitch_api.current_user') as who:
            result = self.bg(self.service.refresh_clips, '2026-10-17T00:00:00Z', '2026-10-19T00:00:00Z', cancel)
            self.assertTrue(result.cancelled)
            who.assert_not_called()
        pages = [TwitchClipPage((clip('BeforeFailure'),), 'later'), RuntimeError('authored network failure')]
        with patch('native.services.twitch_api.current_user', return_value={'id': '42', 'display_name': 'Creator'}), \
                patch('native.services.twitch_api.clips_page', side_effect=pages), self.assertRaisesRegex(RuntimeError, 'authored'):
            self.bg(self.service.refresh_clips, '2026-10-17T00:00:00Z', '2026-10-19T00:00:00Z', threading.Event())
        self.assertIn('BeforeFailure', [row.id for row in self.bg(self.service.page, 'clips').entries])

    def test_repeated_cursor_stops_without_losing_saved_pages(self):
        repeated = TwitchClipPage((clip('Repeated'),), 'same')
        with patch('native.services.twitch_api.current_user', return_value={'id': '42', 'display_name': 'Creator'}), \
                patch('native.services.twitch_api.clips_page', return_value=repeated) as fetch, \
                self.assertRaisesRegex(ValueError, 'repeated a page cursor'):
            self.bg(self.service.refresh_clips, '2026-10-17T00:00:00Z', '2026-10-19T00:00:00Z', threading.Event())
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('Repeated', [row.id for row in self.bg(self.service.page, 'clips').entries])

    def test_auth_result_does_not_return_session_tokens_to_ui(self):
        cancel = threading.Event()
        with patch('native.services.twitch_auth.client_id', return_value='public-client'), \
                patch('native.services.twitch_auth.loopback_login', return_value={'access_token': 'fixture-private-token'}) as login:
            result = self.bg(self.service.connect_twitch, None, cancel)
        self.assertIsInstance(result, str)
        self.assertNotIn('fixture-private-token', result)
        login.assert_called_once_with(None, cancel)


class WorkspaceQtTests(WorkspaceFixture):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def drain(self, window):
        deadline = time.monotonic() + 10
        while window.runner._jobs:
            self.app.processEvents()
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.001)

    def test_real_background_window_keeps_old_kit_and_moment_selection_explicit(self):
        from native.ui.creator_library import CreatorLibraryWindow
        opened = []
        window = CreatorLibraryWindow(self.root, open_moment=lambda *args: opened.append(args))
        window.show()
        try:
            self.drain(window)
            self.assertEqual(window.kind, 'clips')
            self.assertEqual(window.model.rowCount(), 2)
            window.load_view('moments')
            self.drain(window)
            window.table.selectRow(0)
            window.open.click()
            self.assertEqual(opened, [(self.first.id, 1)])
            window.load_view('kits')
            self.drain(window)
            window.table.selectRow(0)
            self.drain(window)
            window.versions.click()
            self.drain(window)
            window.table.selectRow(0)
            self.drain(window)
            self.assertEqual(window.selected_entry().revision, 1)
            self.assertIn('Original Kit\nVersion: 1', window.details.toPlainText())
            window.search.setText('no match')
            self.app.processEvents()
            self.assertEqual(window.proxy.rowCount(), 0)
            self.assertFalse(window.versions.isVisible())
        finally:
            self.drain(window)
            window.close()
            self.app.processEvents()

    def test_late_page_cannot_replace_new_view_or_enable_old_selection_action(self):
        from native.ui.creator_library import CreatorLibraryWindow
        release, started = threading.Event(), threading.Event()
        service = self.service

        def delayed(kind, *args, **kwargs):
            if kind == 'clips':
                started.set()
                if not release.wait(5):
                    raise RuntimeError('test gate timeout')
            return WorkspaceLibrary.page(service, kind, *args, **kwargs)

        with patch.object(service, 'page', side_effect=delayed), \
                patch('native.ui.creator_library.WorkspaceLibrary', return_value=service):
            window = CreatorLibraryWindow(self.root)
            window.show()
            try:
                self.assertTrue(started.wait(3))
                window.load_view('assets')
                deadline = time.monotonic() + 5
                while window._loading:
                    self.app.processEvents()
                    self.assertLess(time.monotonic(), deadline)
                window.table.selectRow(0)
                self.assertTrue(window.verify.isEnabled())
                window.load_view('moments')
                self.assertIsNone(window.selected_entry())
                self.assertFalse(window.verify.isEnabled())
                release.set()
                self.drain(window)
                self.assertEqual(window.kind, 'moments')
                self.assertEqual(window.model.kind, 'moments')
                self.assertEqual(window.model.entries[0].id, self.first.id)
            finally:
                release.set()
                self.drain(window)
                window.close()
                self.app.processEvents()


if __name__ == '__main__':
    unittest.main(verbosity=2)
