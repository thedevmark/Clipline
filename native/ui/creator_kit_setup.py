"""Reusable Kit setup with visual regions over a bounded representative image."""
from copy import deepcopy
from pathlib import Path
import uuid

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QImageReader, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from native.services.creator_kit_setup import CreatorKitSetup, KitSetup
from native.ui import theme


def label(text):
    result = QLabel(text)
    result.setWordWrap(True)
    result.setTextFormat(Qt.TextFormat.PlainText)
    return result


def read_reference(path):
    """Keep the original raster dimensions; never read video or call a decoder worker."""
    source = Path(path)
    if source.suffix.lower() not in ('.png', '.jpg', '.jpeg') or source.stat().st_size > 32 * 1024**2:
        raise ValueError('Choose a PNG or JPEG image smaller than 32 MiB.')
    reader = QImageReader(str(source))
    reader.setAutoTransform(False)
    if bytes(reader.format()).lower() not in (b'png', b'jpeg', b'jpg'):
        raise ValueError('Choose an image encoded as PNG or JPEG.')
    dimensions = reader.size()
    width, height = dimensions.width(), dimensions.height()
    if not (0 < width <= 16384 and 0 < height <= 16384 and width * height <= 32_000_000):
        raise ValueError('Choose a reference image with at most 32 million pixels and 16384 pixels per side.')
    reader.setScaledSize(dimensions.scaled(QSize(1280, 800), Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    if image.isNull():
        raise ValueError('The image could not be read. Choose another PNG or JPEG image.')
    return QPixmap.fromImage(image), width, height


class RegionCanvas(QWidget):
    """Visual guide derived from FacecamGuideDialog, with clamped letterbox geometry."""
    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(240)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName('OBS scene regions. Draw a region. Arrow keys move it; Shift and arrows resize it.')
        self.frame = QPixmap()
        self.source_size = QSize()
        self.regions = {}
        self.role = 'gameplay'
        self.anchor = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_scene(self, frame, width, height, regions):
        self.frame = frame
        self.source_size = QSize(width, height)
        self.regions = deepcopy(regions)
        self.anchor = None
        self.update()

    def image_rect(self):
        if self.frame.isNull() or self.source_size.isEmpty():
            return QRectF()
        scale = min(self.width() / self.source_size.width(), self.height() / self.source_size.height())
        width, height = self.source_size.width() * scale, self.source_size.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def normalized(self, point):
        rect = self.image_rect()
        return QPointF(max(0., min(1., (point.x() - rect.x()) / rect.width())),
                       max(0., min(1., (point.y() - rect.y()) / rect.height())))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.BG_INK))
        image = self.image_rect()
        if image.isEmpty():
            painter.setPen(QColor(theme.INK_MUTED))
            painter.drawText(self.rect().adjusted(24, 24, -24, -24),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             'Choose a representative OBS image to draw this scene’s regions.')
            return
        painter.drawPixmap(image.toRect(), self.frame)
        for role in ('gameplay', 'facecam'):
            region = self.regions.get(role)
            if region is None:
                continue
            rect = QRectF(image.x() + region['x'] * image.width(), image.y() + region['y'] * image.height(),
                          region['w'] * image.width(), region['h'] * image.height())
            color = QColor(theme.ACCENT if role == 'gameplay' else theme.PURPLE)
            painter.setPen(QPen(color, 3 if self.role == role else 1.5))
            tint = QColor(color)
            tint.setAlpha(26)
            painter.fillRect(rect, tint)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
            caption_y = rect.bottom() - 25 if role == 'gameplay' else rect.y()
            painter.fillRect(QRectF(rect.x(), caption_y, 95, 25), QColor(theme.BG_INK))
            painter.drawText(QRectF(rect.x() + 5, caption_y, 90, 25), Qt.AlignmentFlag.AlignVCenter, role.capitalize())
        if self.hasFocus():
            painter.setPen(QPen(QColor(theme.ACCENT), 1, Qt.PenStyle.DashLine))
            painter.drawRect(self.rect().adjusted(1, 1, -2, -2))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.image_rect().contains(event.position()):
            return
        self.setFocus()
        self.anchor = self.normalized(event.position())

    def mouseMoveEvent(self, event):
        if self.anchor is None:
            return
        end = self.normalized(event.position())
        self.regions[self.role] = {'x': min(end.x(), self.anchor.x()), 'y': min(end.y(), self.anchor.y()),
                                   'w': abs(end.x() - self.anchor.x()), 'h': abs(end.y() - self.anchor.y())}
        self.changed.emit(deepcopy(self.regions))
        self.update()

    def mouseReleaseEvent(self, event):
        if self.anchor is not None:
            self.mouseMoveEvent(event)
            self.anchor = None

    def keyPressEvent(self, event):
        region = self.regions.get(self.role)
        deltas = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                  Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if region is None or event.key() not in deltas or self.source_size.isEmpty():
            return super().keyPressEvent(event)
        dx, dy = deltas[event.key()]
        dx /= self.source_size.width()
        dy /= self.source_size.height()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            region['w'] = max(1 / self.source_size.width(), min(1 - region['x'], region['w'] + dx))
            region['h'] = max(1 / self.source_size.height(), min(1 - region['y'], region['h'] + dy))
        else:
            region['x'] = max(0., min(1 - region['w'], region['x'] + dx))
            region['y'] = max(0., min(1 - region['h'], region['y'] + dy))
        self.changed.emit(deepcopy(self.regions))
        self.update()


class KitSetupDialog(QDialog):
    """Cancel is metadata-only. Persistence starts only through the shared action owner."""
    def __init__(self, setup, service, runner, parent=None):
        super().__init__(parent)
        self.setup, self.service, self.runner = setup, service, runner
        self.profiles = deepcopy(setup.descriptor['layout_profiles'])
        self.saved_profile_ids = {profile['id'] for profile in self.profiles}
        self.images = {}
        self.current = -1
        self.template_path = setup.template_path
        self.result_setup = None
        self.busy = False
        self.setWindowTitle('New Creator Kit' if setup.kit_id is None else f'Revise Creator Kit · version {setup.version}')
        self.resize(980, 760)
        self.setMinimumSize(700, 500)
        self.setStyleSheet(theme.GLOBAL_QSS + f'''
            QDialog {{ background: {theme.BG_INK}; }}
            QWidget {{ font-size: 16px; }}
            QComboBox {{ background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT};
                border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 10px; }}
            QComboBox:focus {{ border-color: {theme.ACCENT}; }}
            QComboBox QAbstractItemView {{ background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT};
                selection-background-color: {theme.ACCENT_DEEP}; }}
        ''')
        body = QVBoxLayout(self)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName('stage')
        self.scroll.setWidget(content)
        body.addWidget(self.scroll)
        form = QVBoxLayout(content)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(12)
        title = label('Creator Kit')
        title.setProperty('title', True)
        form.addWidget(title)
        form.addWidget(label('Import your editing template once. New Moments reference a saved Kit version.'))
        fields = QFormLayout()
        self.name = QLineEdit(setup.descriptor['name'])
        self.name.setMaxLength(512)
        fields.addRow('Kit &name', self.name)
        form.addLayout(fields)
        template_row = QHBoxLayout()
        self.template = label('')
        self.template.setAccessibleName('Shared Filmcraft editing template')
        template_row.addWidget(self.template, 1)
        self.choose_template = QPushButton('Choose template…')
        self.choose_template.clicked.connect(self._choose_template)
        template_row.addWidget(self.choose_template)
        form.addLayout(template_row)
        self._template_label()
        notice = ('The template is checked when the first native Moment is created. Saving this Kit does not validate its timeline.')
        if setup.kit_id is not None and not setup.native_supported:
            notice += (' This Kit has properties or asset roles the native factory cannot apply. They remain saved; native Moment creation is unavailable for that state.')
        form.addWidget(label(notice))
        self.vocabulary = QPlainTextEdit('\n'.join(setup.descriptor['vocabulary']))
        self.vocabulary.setAccessibleName('Vocabulary, one name or game term per line')
        self.vocabulary.setPlaceholderText('One name or game term per line')
        self.vocabulary.setMaximumHeight(90)
        form.addWidget(label('Vocabulary'))
        form.addWidget(self.vocabulary)
        scene_row = QHBoxLayout()
        self.scenes = QComboBox()
        self.scenes.setAccessibleName('Saved OBS scene profiles')
        self.scenes.currentIndexChanged.connect(self._select_profile)
        scene_row.addWidget(self.scenes, 1)
        self.add_scene = QPushButton('Add OBS scene')
        self.add_scene.clicked.connect(self._add_profile)
        scene_row.addWidget(self.add_scene)
        self.discard_scene = QPushButton('Discard new scene')
        self.discard_scene.clicked.connect(self._discard_profile)
        scene_row.addWidget(self.discard_scene)
        form.addLayout(scene_row)
        scene_fields = QFormLayout()
        self.scene_key = QLineEdit()
        self.scene_key.setMaxLength(512)
        self.scene_key.setPlaceholderText('Exact scene name, for example Gameplay with facecam')
        self.scene_key.textChanged.connect(self._scene_changed)
        scene_fields.addRow('OBS &scene', self.scene_key)
        form.addLayout(scene_fields)
        controls = QHBoxLayout()
        self.image_button = QPushButton('Choose OBS image…')
        self.image_button.clicked.connect(self._choose_image)
        controls.addWidget(self.image_button)
        self.role = QComboBox()
        self.role.addItems(['Gameplay', 'Facecam'])
        self.role.setAccessibleName('Region to draw')
        self.role.currentTextChanged.connect(self._role_changed)
        controls.addWidget(self.role)
        controls.addStretch()
        form.addLayout(controls)
        self.image_info = label('')
        form.addWidget(self.image_info)
        self.canvas = RegionCanvas()
        self.canvas.changed.connect(self._regions_changed)
        form.addWidget(self.canvas)
        form.addWidget(label('Select Gameplay or Facecam, then drag its rectangle. Arrow keys move the selected region; Shift and arrows resize it. '
                             'Scene names identify layouts. The image is setup input and is not added to the Kit.'))
        self.message = label('')
        body.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setProperty('primary', True)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)
        for profile in self.profiles:
            self.scenes.addItem(profile['scene_key'], profile['id'])
        if not self.profiles:
            self._add_profile()
        elif self.current == -1:
            self._select_profile(0)
        self.name.setFocus()

    def _template_label(self):
        self.template.setText('Template: ' + (Path(self.template_path).name if self.template_path else 'Choose a Filmcraft .fcproj file'))
        self.template.setToolTip(self.template_path)

    def _choose_template(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose shared Filmcraft editing template', '', 'Filmcraft project (*.fcproj)')
        if path:
            self.template_path = path
            self._template_label()

    def _add_profile(self):
        profile = {'id': str(uuid.uuid4()), 'scene_key': '', 'source_width': 0, 'source_height': 0, 'regions': {}}
        self.profiles.append(profile)
        self.scenes.addItem('New OBS scene', profile['id'])
        self.scenes.setCurrentIndex(len(self.profiles) - 1)
        self.scene_key.setFocus()

    def _select_profile(self, index):
        if not 0 <= index < len(self.profiles):
            return
        self.current = index
        profile = self.profiles[index]
        self.discard_scene.setVisible(len(self.profiles) > 1 and profile['id'] not in self.saved_profile_ids)
        self.scene_key.blockSignals(True)
        self.scene_key.setText(profile['scene_key'])
        self.scene_key.blockSignals(False)
        frame = self.images.get(profile['id'], QPixmap())
        self.canvas.set_scene(frame, profile['source_width'], profile['source_height'], profile['regions'])
        self.image_info.setText(f'{profile["source_width"]} × {profile["source_height"]} source pixels' if profile['source_width'] else 'Choose an image from this OBS scene.')
        editable = profile['id'] not in self.saved_profile_ids or {'gameplay', 'facecam'} <= set(profile['regions'])
        for widget in (self.scene_key, self.image_button, self.role, self.canvas):
            widget.setEnabled(editable)
        if not editable:
            self.image_info.setText('This saved scene uses other region roles and remains unchanged. Add a new OBS scene to draw Gameplay and Facecam.')

    def _discard_profile(self):
        if (self.current < 0 or len(self.profiles) < 2
                or self.profiles[self.current]['id'] in self.saved_profile_ids):
            return
        profile = self.profiles.pop(self.current)
        self.images.pop(profile['id'], None)
        self.scenes.blockSignals(True)
        self.scenes.removeItem(self.current)
        self.scenes.blockSignals(False)
        self._select_profile(self.scenes.currentIndex())

    def _scene_changed(self, value):
        if self.current >= 0:
            self.profiles[self.current]['scene_key'] = value
            self.scenes.setItemText(self.current, value or 'New OBS scene')

    def _role_changed(self, value):
        self.canvas.role = value.lower()
        self.canvas.update()

    def _regions_changed(self, regions):
        if self.current >= 0:
            self.profiles[self.current]['regions'] = deepcopy(regions)

    def _choose_image(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose representative OBS image', '', 'Images (*.png *.jpg *.jpeg)')
        if path:
            self.set_image(path)

    def set_image(self, path):
        try:
            frame, width, height = read_reference(path)
        except (OSError, ValueError) as exc:
            self.message.setText(str(exc))
            return False
        profile = self.profiles[self.current]
        profile.update(source_width=width, source_height=height)
        self.images[profile['id']] = frame
        self.canvas.set_scene(frame, width, height, profile['regions'])
        self.image_info.setText(f'{width} × {height} source pixels · {Path(path).name}')
        self.message.setText('Check both regions against this image before saving.')
        return True

    def _save(self):
        if self.busy:
            return
        vocabulary = self.vocabulary.toPlainText().splitlines()
        if not self.name.text().strip():
            self.message.setText('Enter a Kit name.')
            self.name.setFocus()
            return
        if any(not term.strip() for term in vocabulary):
            self.message.setText('Remove blank vocabulary lines, or leave Vocabulary empty.')
            self.vocabulary.setFocus()
            return
        self.busy = True
        self.scroll.setEnabled(False)
        self.buttons.setEnabled(False)
        self.message.setText('Checking the shared template and saving a new immutable Kit version…')
        arguments = dict(name=self.name.text(), vocabulary=vocabulary, profiles=deepcopy(self.profiles),
                         template_path=self.template_path, kit_id=self.setup.kit_id, version=self.setup.version)
        try:
            self.runner.run(lambda job: self.service.save(**arguments), on_finished=self._saved, on_error=self._failed)
        except Exception as exc:
            self._failed(str(exc))

    def _saved(self, setup):
        self.result_setup = setup
        self.busy = False
        self.accept()

    def _failed(self, error):
        self.busy = False
        self.scroll.setEnabled(True)
        self.buttons.setEnabled(True)
        self.message.setText(error)

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)


class KitSetupBinding:
    """One modal metadata authoring flow under the existing shared action token."""
    def __init__(self, window):
        self.window = window
        self.service = CreatorKitSetup(window.library)
        self.new = QPushButton('New Creator Kit')
        self.new.setProperty('primary', True)
        self.new.clicked.connect(self.create)
        self.revise = QPushButton('Revise selected version')
        self.revise.clicked.connect(self.edit)
        row = QHBoxLayout()
        row.addWidget(self.new)
        row.addWidget(self.revise)
        row.addStretch()
        self.panel = QWidget(window)
        self.panel.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        window.content_layout.insertWidget(3, self.panel)
        self.dialog = None
        self.active = False
        self._gate_changed()

    def _gate_changed(self):
        window = self.window
        visible = window.kind in ('kits', 'versions')
        self.panel.setVisible(visible)
        available = window.action_allowed(self) and not self.active
        self.new.setEnabled(available)
        self.revise.setEnabled(available and window.selected_entry() is not None)

    def create(self):
        if self.active or not self.window.acquire_action(self):
            return
        self.active = True
        self._show(KitSetup({'name': '', 'asset_refs': {}, 'properties': {}, 'layout_profiles': [], 'vocabulary': []}, ''))

    def edit(self):
        entry = self.window.selected_entry()
        if self.active or self.window.kind not in ('kits', 'versions') or entry is None or not self.window.acquire_action(self):
            return
        self.active = True
        self._gate_changed()
        try:
            self.window.runner.run(lambda job: self.service.load(entry.id, entry.revision),
                                   on_finished=self._show, on_error=self._load_failed)
        except Exception as exc:
            self._load_failed(str(exc))

    def _show(self, setup):
        self.dialog = KitSetupDialog(setup, self.service, self.window.runner, self.window)
        self.dialog.finished.connect(self._finished)
        self._gate_changed()
        self.dialog.open()

    def _finished(self, result):
        dialog, self.dialog = self.dialog, None
        self.active = False
        setup = dialog.result_setup
        self.window.release_action(self)
        if setup is not None:
            self.window.message.setText(f'{setup.descriptor["name"]} · version {setup.version} saved. Existing Moments keep their pinned Kit version.')
            self.window.load_view('kits')
        dialog.deleteLater()

    def _load_failed(self, error):
        self.active = False
        self.window.release_action(self)
        self.window.message.setText(error)
