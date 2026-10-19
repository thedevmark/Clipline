"""Explicit runtime configuration cannot silently select or replace a program."""
import json
from pathlib import Path
import tempfile
import unittest

from native.services.editor_runtime import load_editor_runtime


class EditorRuntimeTests(unittest.TestCase):
    def test_explicit_program_identities_survive_loading_without_binary_io(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            program = {'path': str(root/'uninstalled.exe'), 'sha256': 'a'*64, 'version': 'authored-fixture-only'}
            value = {'schema': 1, 'creator_kit_helper': program, 'editor': program,
                     'python': str(root/'python.exe')}
            path = root/'runtime.json'
            path.write_text(json.dumps(value))
            runtime = load_editor_runtime(path)
            self.assertEqual(runtime.helper.path, root/'uninstalled.exe')
            self.assertEqual(runtime.editor.sha256, 'a'*64)
            self.assertEqual(runtime.python, root/'python.exe')
            for key, invalid in (('path', 'filmcraft.exe'), ('sha256', 'unverified'), ('version', '')):
                with self.subTest(field=key):
                    value['editor'] = {**program, key: invalid}
                    path.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        load_editor_runtime(path)

    def test_duplicate_schema_cannot_change_the_consumed_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'runtime.json'
            path.write_text('{"schema":1,"schema":2}')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                load_editor_runtime(path)
