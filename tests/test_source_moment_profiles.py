"""Independent source-origin, admission, migration and profile-choice boundaries."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from native.core.creator_library import CreatorLibrary, LibraryError
from native.services import native_moment
from native.ui.source_moment_preparation import MomentPreparationDialog
from native.workers import JobRunner

from tests import test_source_moment_preparation as t
from tests.test_source_moment_preparation import app, case

@pytest.mark.parametrize('fault', ['runtime', 'intent', 'output', 'bytes', 'path'],
    ids=['runtime', 'intent', 'output', 'bytes', 'path'])
def test_self_consistent_receipt_extra_bindings_are_refused(case, fault):
    workspace, service, kit, bg = case
    def change(result):
        if fault == 'runtime': result['runtime'] = {'other': 'fixture'}
        elif fault == 'intent': result['intent_sha256'] = 'f' * 64
        elif fault == 'output': result['output_set_id'] = t.uid()
        elif fault == 'bytes': result['media']['bytes'] += 1
        else: result['media']['path'] = str(workspace.directory / 'unadmitted.mp4')
    t.seed_source(workspace, mutate_result=change)
    with pytest.raises((LibraryError, ValueError)):
        bg(service.inspect, 'Clip-A')
    with CreatorLibrary(workspace.database) as library:
        assert not library.list_moments()['items']

def test_clip_only_origin_and_exact_native_admission_do_not_invent_vod_clock(case, monkeypatch):
    workspace, service, kit, bg = case
    original = t.clip
    monkeypatch.setattr(t, 'clip', lambda identity='Clip-A': replace(original(identity), video_id=None, vod_offset_us=None))
    t.seed_source(workspace)
    context = bg(service.inspect, 'Clip-A')
    choice = bg(service.kit, kit, 1)
    moment = t.create(service, context, choice, bg)
    def admitted():
        with workspace._library() as library:
            origin = library.moment_origin(moment.id)
            value = native_moment.admission(library, moment.id, moment.revision,
                'profile-b', 'OBS facecam close-up')
            return origin, value
    origin, value = bg(admitted)
    assert origin['selected_metadata']['video_id'] is None
    assert origin['selected_metadata']['vod_offset_us'] is None
    assert value.moment.source_in_us == 250001 and value.moment.source_out_us == 1750001
    assert value.kit.version == 1 and value.profile['id'] == 'profile-b'

def test_schema4_preserves_old_version_and_revision_history(tmp_path):
    path = tmp_path / 'old.sqlite'
    source = tmp_path / 'old.fixture'; source.write_bytes(b'independent old source')
    moment_id, _ = t.seed_schema3_fixture(path, source, properties={'tone':'old'},
        overrides={'tone':'manual revised'}, revision=2, second_kit_name='Later Kit')
    with t.sqlite3.connect(path) as database:
        database.execute('UPDATE moments SET title=?,source_in_us=? WHERE id=?',('Old pinned Moment',10,moment_id))
        before = {table: [tuple(row) for row in database.execute('SELECT * FROM ' + table)]
            for table in ('assets', 'kits', 'kit_versions', 'moments')}
    with CreatorLibrary(path) as library:
        assert library.connection.execute('PRAGMA user_version').fetchone()[0] == 4
        assert library.moment_origin(moment_id) is None
        assert library.moment(moment_id).kit_version == 1
        assert library.moment(moment_id).revision == 2
        for table, expected in before.items():
            assert [tuple(row) for row in library.connection.execute('SELECT * FROM ' + table)] == expected

def test_duplicate_scene_labels_distinguish_exact_profiles(app, case):
    workspace, service, kit, bg = case
    with CreatorLibrary(workspace.database) as library:
        descriptor = library.kit(kit, 2)
        first, second = t.profile('crop-a', 'OBS shared'), t.profile('crop-b', 'OBS shared')
        second['regions']['facecam'] = {'x': .7, 'y': .7, 'w': .2, 'h': .2}
        descriptor['layout_profiles'] = [first, second]
        library.revise_kit(kit, descriptor, expected_version=2)
        assert library.kit(kit, 3)['layout_profiles'][0]['regions'] != library.kit(kit, 3)['layout_profiles'][1]['regions']
    t.seed_source(workspace)
    context = bg(service.inspect, 'Clip-A')
    runner = JobRunner()
    dialog = MomentPreparationDialog(context, service, runner)
    dialog.show()
    t.wait(app, lambda: not runner._jobs and not dialog.busy)
    labels = [dialog.scenes.itemText(index) for index in range(dialog.scenes.count())]
    identities = [dialog.scenes.itemData(index)[0] for index in range(dialog.scenes.count())]
    dialog.reject()
    layout = t.LayoutSelection(SimpleNamespace(title='Exact profile choice', kit_name='Kit', kit_version=3,
        profiles=(('crop-a', 'OBS shared'), ('crop-b', 'OBS shared')), selected_layout=None))
    editor_labels = [layout.layouts.itemText(index) for index in range(layout.layouts.count())]
    layout.reject()
    assert identities == ['crop-a', 'crop-b']
    assert len(set(labels)) == len(labels) and len(set(editor_labels)) == len(editor_labels), \
        f'Exact profiles have indistinguishable labels: prepare={labels}, editor={editor_labels}'


def duplicate_context(case, scene='OBS shared'):
    workspace, service, kit, bg = case
    with CreatorLibrary(workspace.database) as library:
        descriptor = library.kit(kit, 2)
        first, second = t.profile('crop-a', scene), t.profile('crop-b', scene)
        second['regions']['facecam'] = {'x':.7,'y':.7,'w':.2,'h':.2}
        library.revise_kit(kit,{**descriptor,'layout_profiles':[first,second]},expected_version=2)
    t.seed_source(workspace)
    return bg(service.inspect,'Clip-A')


def test_duplicate_labels_keep_exact_creation_and_editor_preselection(app, case):
    workspace, service, kit, bg = case
    context = duplicate_context(case)
    runner = JobRunner()
    dialog = MomentPreparationDialog(context,service,runner)
    dialog.show()
    t.wait(app,lambda:not runner._jobs and not dialog.busy)
    dialog.scenes.setCurrentIndex(1)
    assert dialog.scenes.currentData()[:2] == ('crop-b','OBS shared')
    assert 'crop-b' in dialog.scenes.currentText() and 'OBS shared' in dialog.scenes.currentText()
    dialog.save_button.click()
    t.wait(app,lambda:not runner._jobs and dialog.created is not None)
    with CreatorLibrary(workspace.database) as library:
        assert library.moment_origin(dialog.created.id)['profile_id'] == 'crop-b'
    edit = bg(workspace.moment_edit_context,dialog.created.id,dialog.created.revision)
    layout = t.LayoutSelection(edit)
    assert layout.selected_layout() == ('crop-b','OBS shared')
    assert 'crop-b' in layout.layouts.currentText() and 'OBS shared' in layout.layouts.currentText()
    layout.reject()


def test_duplicate_long_scene_names_remain_bounded_and_identifiable(app, case, tmp_path):
    from PySide6.QtCore import Qt
    workspace, service, kit, bg = case
    scene = 'OBS shared ' + 'long scene name ' * 24
    context = duplicate_context(case,scene)
    runner = JobRunner()
    dialog = MomentPreparationDialog(context,service,runner)
    dialog.resize(760,600)
    dialog.show()
    t.wait(app,lambda:not runner._jobs and not dialog.busy)
    assert dialog.width() == 760
    assert dialog.scroll.horizontalScrollBar().maximum() == 0
    assert dialog.scenes.width() <= dialog.scroll.viewport().width()
    for index,profile in enumerate(('crop-a','crop-b')):
        assert dialog.scenes.itemText(index).startswith(profile)
        tooltip = dialog.scenes.itemData(index,Qt.ItemDataRole.ToolTipRole)
        assert profile in tooltip and scene in tooltip
    assert dialog.grab().save(str(tmp_path/'profiles-preparation-760.png'))
    dialog.reject()
    layout = t.LayoutSelection(SimpleNamespace(title='Exact profile choice',kit_name='Kit',kit_version=3,
        profiles=(('crop-a',scene),('crop-b',scene)),selected_layout=('crop-b',scene)))
    layout.resize(760,260)
    layout.show()
    app.processEvents()
    assert layout.width() == 760 and layout.layouts.width() < 760
    assert layout.layouts.currentText().startswith('crop-b')
    assert layout.selected_layout() == ('crop-b',scene)
    assert layout.grab().save(str(tmp_path/'profiles-editor-760.png'))
    layout.reject()
