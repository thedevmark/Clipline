from concurrent.futures import ThreadPoolExecutor
import base64
from copy import deepcopy
import os
from pathlib import Path
import time
from unittest.mock import patch

import pytest
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
pytest.importorskip('PySide6')
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QFontDatabase, QImage, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog

from native.core.creator_library import CreatorLibrary, LibraryError, RevisionConflict
from native.services.creator_kit_setup import CreatorKitSetup, KitSetup
from native.services.native_moment import admission
from native.services.workspace_library import WorkspaceLibrary
from native.ui.creator_kit_setup import KitSetupDialog, RegionCanvas, read_reference
from native.ui.creator_library import CreatorLibraryWindow
from native.workers import JobRunner

def profile(identity='scene-one', scene='Gameplay scene'):
    return {'id': identity, 'scene_key': scene, 'source_width': 3840, 'source_height': 2160,
            'regions': {'gameplay': {'x': 0., 'y': 0., 'w': 1., 'h': 1.},
                        'facecam': {'x': .05, 'y': .05, 'w': .2, 'h': .2}}}


def empty_setup():
    return KitSetup({'name': '', 'asset_refs': {}, 'properties': {}, 'layout_profiles': [], 'vocabulary': []}, '')


@pytest.fixture(scope='session')
def app():
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(False)
    # The offscreen platform does not discover Windows fonts automatically.
    font = Path('C:/Windows/Fonts/segoeui.ttf')
    if font.is_file():
        assert QFontDatabase.addApplicationFont(str(font)) >= 0
    return application


@pytest.fixture
def data(tmp_path):
    template = tmp_path / 'Editing template.fcproj'
    template.write_text('Fixture identity only; no native timeline qualification', encoding='utf8')
    workspace = WorkspaceLibrary(tmp_path)
    service = CreatorKitSetup(workspace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def background(fn, *args, **kwargs):
            return pool.submit(fn, *args, **kwargs).result(timeout=10)
        yield workspace, service, template, background


def save(service, template, background, **kwargs):
    arguments = dict(name='Fixture Creator Kit', vocabulary=['deutschmark', 'game term'],
                     profiles=[profile()], template_path=str(template))
    arguments.update(kwargs)
    return background(service.save, **arguments)


def wait(app, condition):
    deadline = time.monotonic() + 8
    while not condition():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError('Qt did not reach the expected operation state')
        time.sleep(.005)
    app.processEvents()


def test_create_reuse_and_native_admission(data):
    workspace, service, template, background = data
    first = save(service, template, background)
    second = save(service, template, background, name='Another Kit')
    with CreatorLibrary(workspace.database) as library:
        assert len(library.list_assets()['items']) == 1
        assert first.descriptor['asset_refs'] == second.descriptor['asset_refs']
        source = template.parent / 'Source identity.fixture'
        source.write_bytes(b'No decoder required for metadata admission')
        asset = library.register_asset(source)
        moment = library.create_moment(title='Fixture moment', source_asset_id=asset.id,
            source_in_us=0, source_out_us=1_000_000, kit_id=first.kit_id, kit_version=1)
        facts = admission(library, moment.id, moment.revision, 'scene-one', 'Gameplay scene')
        assert facts.kit.descriptor_json == library.kit_record(first.kit_id, 1).descriptor_json
        assert facts.template.id == first.descriptor['asset_refs']['native_template']
        with pytest.raises(ValueError, match='exact canonical profile and scene'):
            admission(library, moment.id, moment.revision, 'scene-one', 'Different scene')


def test_revise_preserves_old_bytes_moment_and_foreign_fields(data):
    workspace, service, template, background = data
    with CreatorLibrary(workspace.database) as library:
        asset = library.register_asset(template)
        first_profile = profile()
        first_profile['regions']['uneditable_overlay'] = {'x': .1, 'y': .1, 'w': .5, 'h': .5}
        second_profile = profile('other-scene', 'Other OBS scene')
        descriptor = {'name': 'Original Kit', 'asset_refs': {'native_template': asset.id, 'logo': asset.id},
                      'properties': {'caption.custom': {'size': 43, 'enabled': False}},
                      'layout_profiles': [first_profile, second_profile], 'vocabulary': ['original']}
        kit = library.create_kit(descriptor)
        old = library.kit_record(kit, 1)
        moment = library.create_moment(title='Pinned moment', source_asset_id=asset.id,
            source_in_us=0, source_out_us=5, kit_id=kit, kit_version=1)
    loaded = background(service.load, kit, 1)
    assert not loaded.native_supported
    changed = deepcopy(loaded.descriptor['layout_profiles'])
    changed[0]['regions'].pop('uneditable_overlay')
    changed[0]['regions']['facecam']['x'] = .15
    changed[0]['scene_key'] = 'Changed scene key'
    revised = save(service, template, background, kit_id=kit, version=1, profiles=changed,
                   name='Revised Kit', vocabulary=['new term'])
    assert revised.version == 2
    assert revised.descriptor['properties'] == descriptor['properties']
    assert revised.descriptor['asset_refs'] == descriptor['asset_refs']
    assert revised.descriptor['layout_profiles'][1] == second_profile
    assert revised.descriptor['layout_profiles'][0]['regions']['uneditable_overlay'] == first_profile['regions']['uneditable_overlay']
    with CreatorLibrary(workspace.database) as library:
        assert library.kit_record(kit, 1) == old
        assert library.moment(moment.id).kit_version == 1
        with pytest.raises(ValueError, match='canonical native_template'):
            admission(library, moment.id, moment.revision, first_profile['id'], first_profile['scene_key'])
    with pytest.raises(RevisionConflict, match='newer version'):
        save(service, template, background, kit_id=kit, version=1, profiles=changed)


@pytest.mark.parametrize('case', ['missing', 'changed', 'rect', 'vocabulary', 'scene', 'no-profile'], ids=['missing','changed','rect','vocab','scene','empty'])
def test_failure_does_not_revise(data, case):
    workspace, service, template, background = data
    original = save(service, template, background)
    arguments = dict(kit_id=original.kit_id, version=1)
    if case == 'missing':
        arguments['template_path'] = str(template.with_name('missing.fcproj'))
    elif case == 'changed':
        template.write_text('Changed registered template bytes', encoding='utf8')
    elif case == 'rect':
        bad = profile()
        bad['regions']['facecam']['w'] = 2
        arguments['profiles'] = [bad]
    elif case == 'vocabulary':
        arguments['vocabulary'] = ['valid', ' ']
    elif case == 'scene':
        arguments['profiles'] = [profile(scene='')]
    elif case == 'no-profile':
        arguments['profiles'] = []
    with pytest.raises(LibraryError):
        save(service, template, background, **arguments)
    with CreatorLibrary(workspace.database) as library:
        assert library.list_kits()['items'][0].version == 1
        assert len(library.list_assets()['items']) == 1


def test_background_boundary_and_existing_profile_removal(data):
    workspace, service, template, background = data
    with pytest.raises(RuntimeError, match='background owner'):
        service.save(name='No main-thread hashing', vocabulary=[], profiles=[profile()], template_path=str(template))
    original = save(service, template, background)
    with pytest.raises(LibraryError, match='cannot be removed'):
        save(service, template, background, kit_id=original.kit_id, version=1,
             profiles=[profile('different', 'Different scene')])


def test_uneditable_profile_is_preserved_and_cannot_be_changed(app, data):
    workspace, service, template, background = data
    other = profile('foreign', 'Uneditable scene')
    other['regions'] = {'foreign_role': {'x': .1, 'y': .1, 'w': .3, 'h': .3}}
    with CreatorLibrary(workspace.database) as library:
        asset = library.register_asset(template)
        kit = library.create_kit({'name': 'Other layout', 'asset_refs': {'native_template': asset.id},
            'properties': {}, 'layout_profiles': [other], 'vocabulary': []})
    revised = save(service, template, background, kit_id=kit, version=1, profiles=[other])
    assert revised.descriptor['layout_profiles'] == [other]
    dialog = KitSetupDialog(revised, service, JobRunner())
    assert not dialog.scene_key.isEnabled() and not dialog.image_button.isEnabled()
    dialog._add_profile()
    assert dialog.scene_key.isEnabled() and dialog.image_button.isEnabled()
    dialog.reject()
    altered = deepcopy(other)
    altered['scene_key'] = 'Changed uneditable scene'
    with pytest.raises(LibraryError, match='Leave it unchanged'):
        save(service, template, background, kit_id=kit, version=2, profiles=[altered])


def draw(canvas, start, end):
    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(*start))
    QTest.mouseMove(canvas, QPoint(*end))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(*end))


def test_real_crop_gestures_letterbox_clamp_and_keyboard(app):
    canvas = RegionCanvas()
    frame = QPixmap(1280, 720)
    frame.fill(QColor('#243349'))
    canvas.set_scene(frame, 3840, 2160, {})
    canvas.resize(800, 600)
    canvas.show()
    app.processEvents()
    draw(canvas, (10, 10), (90, 20))
    assert canvas.regions == {}
    draw(canvas, (80, 120), (400, 300))
    assert canvas.regions['gameplay'] == pytest.approx({'x': .1, 'y': .1, 'w': .4, 'h': .4})
    canvas.role = 'facecam'
    draw(canvas, (600, 450), (1000, 700))
    assert canvas.regions['facecam'] == pytest.approx({'x': .75, 'y': 5/6, 'w': .25, 'h': 1/6})
    canvas.role = 'gameplay'
    QTest.keyClick(canvas, Qt.Key.Key_Right)
    assert canvas.regions['gameplay']['x'] == pytest.approx(.1 + 1 / 3840)
    QTest.keyClick(canvas, Qt.Key.Key_Left, Qt.KeyboardModifier.ShiftModifier)
    assert canvas.regions['gameplay']['w'] == pytest.approx(.4 - 1 / 3840)
    canvas.close()


def image_fixture(path, width=3840, height=2160):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor('#243349'))
    assert image.save(str(path))


def test_bounded_reference_original_dimensions_and_failed_image_preserves(app, data):
    workspace, service, template, background = data
    image = template.parent / 'OBS.png'
    image_fixture(image)
    pixmap, width, height = read_reference(image)
    assert (width, height) == (3840, 2160)
    assert pixmap.width() <= 1280 and pixmap.height() <= 800
    dialog = KitSetupDialog(empty_setup(), service, JobRunner())
    assert dialog.set_image(image)
    dialog.profiles[0]['regions'] = deepcopy(profile()['regions'])
    before = deepcopy(dialog.profiles)
    assert not dialog.set_image(template.parent / 'absent.png')
    assert dialog.profiles == before
    bad = template.parent / 'invalid.jpg'
    bad.write_bytes(b'Not a JPEG')
    assert not dialog.set_image(bad)
    assert dialog.profiles == before
    oversized = template.parent / 'too-wide.png'
    image_fixture(oversized, 16385, 1)
    assert not dialog.set_image(oversized)
    assert dialog.profiles == before
    jpeg = template.parent / 'OBS.jpg'
    image_fixture(jpeg, 1920, 1080)
    preview, jw, jh = read_reference(jpeg)
    assert (jw, jh) == (1920, 1080) and preview.width() <= 1280
    disguised = template.parent / 'not-png.png'
    disguised.write_bytes(base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'))
    with pytest.raises(ValueError, match='encoded as PNG or JPEG'):
        read_reference(disguised)
    dialog.reject()


def test_cancel_file_chooser_and_edit_preserve_all_fields(app, data):
    workspace, service, template, background = data
    original = save(service, template, background, profiles=[profile(), profile('second','Second scene')])
    loaded = background(service.load, original.kit_id, 1)
    runner = JobRunner()
    dialog = KitSetupDialog(loaded, service, runner)
    dialog.scene_key.setText('Changed scene')
    dialog.scenes.setCurrentIndex(1)
    dialog.scenes.setCurrentIndex(0)
    assert dialog.scene_key.text() == 'Changed scene'
    with patch.object(QFileDialog, 'getOpenFileName', return_value=('', '')):
        dialog._choose_template()
        dialog._choose_image()
    assert dialog.template_path == str(template)
    assert dialog.profiles[0]['scene_key'] == 'Changed scene'
    dialog._add_profile()
    dialog.scene_key.setText('Accidental scene')
    assert dialog.discard_scene.isVisibleTo(dialog)
    dialog._discard_profile()
    assert len(dialog.profiles) == 2
    assert dialog.profiles[0]['scene_key'] == 'Changed scene'
    dialog.name.setText('Cancelled changes')
    QTest.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.result() == QDialog.DialogCode.Rejected
    assert runner._jobs == []
    with CreatorLibrary(workspace.database) as library:
        assert library.kit(original.kit_id, 1) == loaded.descriptor


def test_dialog_save_actual_background_sqlite_and_failure_recovery(app, data):
    workspace, service, template, background = data
    runner = JobRunner()
    dialog = KitSetupDialog(empty_setup(), service, runner)
    dialog.show()
    dialog.name.setText('New saved Kit')
    dialog.template_path = str(template)
    dialog.profiles = [profile()]
    dialog.vocabulary.setPlainText('valid\n \nterm')
    dialog._save()
    assert not runner._jobs and not dialog.busy
    dialog.vocabulary.setPlainText('valid')
    dialog.template_path = str(template.with_name('missing.fcproj'))
    dialog._save()
    wait(app, lambda: not runner._jobs and not dialog.busy)
    assert dialog.isVisible() and dialog.name.text() == 'New saved Kit'
    assert 'missing or changed' in dialog.message.text()
    dialog.template_path = str(template)
    dialog._save()
    assert dialog.busy
    dialog.reject()
    assert dialog.isVisible()
    wait(app, lambda: not runner._jobs and dialog.result_setup is not None)
    assert dialog.result() == QDialog.DialogCode.Accepted
    with CreatorLibrary(workspace.database) as library:
        assert library.list_kits()['items'][0].name == 'New saved Kit'


def test_window_shared_gate_navigation_and_cancel_no_database_mutation(app, data):
    workspace, service, template, background = data
    runner = JobRunner()
    window = CreatorLibraryWindow(workspace.directory, runner=runner)
    window.show()
    wait(app, lambda: not runner._jobs)
    window.load_view('kits')
    wait(app, lambda: not runner._jobs)
    owner = object()
    assert window.acquire_action(owner)
    assert not window.kit_binding.new.isEnabled()
    window.kit_binding.create()
    assert window.kit_binding.dialog is None
    window.release_action(owner)
    QTest.mouseClick(window.kit_binding.new, Qt.MouseButton.LeftButton)
    assert window._action_owner is window.kit_binding
    assert not window.acquire_action(owner)
    dialog = window.kit_binding.dialog
    QTest.keyClick(dialog.name, Qt.Key.Key_Tab)
    assert dialog.focusWidget() is not None
    dialog.reject()
    app.processEvents()
    assert window._action_owner is None
    with CreatorLibrary(workspace.database) as library:
        assert library.list_kits()['items'] == []
        assert library.list_assets()['items'] == []
    window.close()


def test_first_kit_user_flow_and_revise_selected_version(app, data):
    workspace, service, template, background = data
    runner = JobRunner()
    window = CreatorLibraryWindow(workspace.directory, runner=runner)
    window.show()
    wait(app, lambda: not runner._jobs)
    window.load_view('kits')
    wait(app, lambda: not runner._jobs)
    window.kit_binding.new.click()
    dialog = window.kit_binding.dialog
    dialog.name.setText('First usable Kit')
    dialog.scene_key.setText('OBS gameplay')
    dialog.vocabulary.setPlainText('deutschmark')
    image = template.parent / 'Representative OBS.png'
    image_fixture(image)
    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(template), '')):
        dialog._choose_template()
    with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(image), '')):
        dialog._choose_image()
    dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum())
    app.processEvents()
    image_rect = dialog.canvas.image_rect()
    def at(x, y):
        return round(image_rect.x() + x * image_rect.width()), round(image_rect.y() + y * image_rect.height())
    draw(dialog.canvas, at(.02,.02), at(.98,.98))
    dialog.role.setCurrentText('Facecam')
    draw(dialog.canvas, at(.05,.05), at(.25,.25))
    dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
    wait(app, lambda: not runner._jobs and window.kit_binding.dialog is None)
    assert window.kind == 'kits' and window.model.rowCount() == 1
    window.table.selectRow(0)
    wait(app, lambda: not runner._jobs)
    window.kit_binding.revise.click()
    wait(app, lambda: not runner._jobs and window.kit_binding.dialog is not None)
    revised = window.kit_binding.dialog
    assert revised.setup.version == 1
    assert revised.profiles[0]['scene_key'] == 'OBS gameplay'
    assert revised.template_path == str(template)
    revised.name.setText('Revised usable Kit')
    revised.buttons.button(QDialogButtonBox.StandardButton.Save).click()
    wait(app, lambda: not runner._jobs and window.kit_binding.dialog is None)
    assert window.model.entries[0].revision == 2
    with CreatorLibrary(workspace.database) as library:
        summary = library.list_kits()['items'][0]
        assert library.kit(summary.id,1)['name'] == 'First usable Kit'
        assert library.kit(summary.id,2)['name'] == 'Revised usable Kit'
        assert len(library.list_assets()['items']) == 1
    window.close()


@pytest.mark.parametrize('width,height', [(760,600),(1180,780)], ids=['760','1180'])
def test_render_narrow_and_wide_no_horizontal_overflow(app, data, tmp_path, width, height):
    workspace, service, template, background = data
    setup = save(service, template, background, profiles=[profile(),profile('second','Other OBS scene')])
    runner = JobRunner()
    dialog = KitSetupDialog(setup, service, runner)
    image = template.parent / 'OBS fixture.png'
    image_fixture(image)
    dialog.set_image(image)
    dialog.resize(width,height)
    dialog.show()
    app.processEvents()
    assert dialog.width() == width
    assert dialog.scroll.horizontalScrollBar().maximum() == 0
    assert dialog.buttons.geometry().bottom() < dialog.height()
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).isVisible()
    assert dialog.grab().save(str(tmp_path / f'kit-setup-{width}.png'))
    dialog.scroll.verticalScrollBar().setValue(dialog.scroll.verticalScrollBar().maximum())
    app.processEvents()
    assert dialog.grab().save(str(tmp_path / f'kit-setup-{width}-regions.png'))
    dialog.reject()
