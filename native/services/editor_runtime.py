"""Explicit local runtime configuration for the opt-in native companion."""
from dataclasses import dataclass
import json
from pathlib import Path

from native.services.filmcraft_session import QualifiedEditor
from native.services.native_moment import QualifiedCreatorKitHelper


@dataclass(frozen=True)
class EditorRuntime:
    helper: QualifiedCreatorKitHelper
    editor: QualifiedEditor
    python: Path
    managed_exit: bool = False

    def controller(self, workspace, *, caption_runtime=None, export_runtime=None):
        from native.services.workspace_editor import WorkspaceEditorController
        return WorkspaceEditorController(workspace, helper=self.helper, editor=self.editor,
                                         python=self.python, managed_exit=self.managed_exit, caption_runtime=caption_runtime, export_runtime=export_runtime)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('The editor runtime contains a duplicate setting.')
        value[key] = item
    return value


def load_editor_runtime(path):
    with Path(path).open('rb') as stream:
        encoded = stream.read(65537)
    if len(encoded) > 65536:
        raise ValueError('The editor runtime configuration exceeds 64 KiB.')
    value = json.loads(encoded, object_pairs_hook=_object)
    if not isinstance(value, dict) or type(value.get('schema')) is not int:
        raise ValueError('Unsupported editor runtime configuration.')
    if value['schema'] == 1:
        expected = {'schema', 'creator_kit_helper', 'editor', 'python'}
        managed_exit = False
    elif value['schema'] == 2:
        expected = {'schema', 'creator_kit_helper', 'editor', 'python', 'managed_exit_protocol'}
        if type(value.get('managed_exit_protocol')) is not int or value['managed_exit_protocol'] != 1:
            raise ValueError('The runtime requires the qualified managed-exit protocol.')
        managed_exit = True
    else:
        raise ValueError('Unsupported editor runtime configuration.')
    if set(value) != expected:
        raise ValueError('Unsupported editor runtime configuration fields.')
    programs = []
    for name, cls in (('creator_kit_helper', QualifiedCreatorKitHelper), ('editor', QualifiedEditor)):
        record = value[name]
        if not isinstance(record, dict) or set(record) != {'path', 'sha256', 'version'}:
            raise ValueError('The editor runtime requires explicit program identities.')
        if not isinstance(record['path'], str) or not Path(record['path']).is_absolute():
            raise ValueError('The editor runtime requires absolute program paths.')
        if (not isinstance(record['sha256'], str) or len(record['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in record['sha256'])):
            raise ValueError('The editor runtime requires a SHA256 for each program.')
        if not isinstance(record['version'], str) or not record['version'].strip() or len(record['version']) > 128:
            raise ValueError('The editor runtime requires a bounded program version.')
        programs.append(cls(Path(record['path']), record['sha256'], record['version']))
    if not isinstance(value['python'], str) or not Path(value['python']).is_absolute():
        raise ValueError('The editor runtime requires an absolute Python path.')
    return EditorRuntime(programs[0], programs[1], Path(value['python']), managed_exit)
