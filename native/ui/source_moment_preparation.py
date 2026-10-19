"""Choose a saved source range and exact Kit pins before opening the existing editor."""
from collections import Counter
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from native.services.source_moment_preparation import SourceMomentPreparation
from native.ui import theme


def label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    return widget


class MomentPreparationDialog(QDialog):
    def __init__(self, context, service, runner, parent=None):
        super().__init__(parent)
        self.context, self.service, self.runner = context, service, runner
        self.busy = False
        self.choice = None
        self.created = None
        self.kit_cursor = None
        self.version_cursor = None
        self.version_names = {}
        self.setWindowTitle('Prepare Moment')
        self.resize(920, 700)
        self.setMinimumSize(700, 500)
        self.setStyleSheet(theme.GLOBAL_QSS + f'''
            QDialog {{ background: {theme.BG_INK}; }}
            QWidget {{ font-size: 16px; }}
            QComboBox, QDoubleSpinBox {{ background: {theme.BG_PANEL}; color: {theme.INK_BRIGHT};
                border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 10px; }}
            QComboBox:focus, QDoubleSpinBox:focus {{ border-color: {theme.ACCENT}; }}
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
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(14)
        heading = label('Prepare Moment')
        heading.setProperty('title', True)
        layout.addWidget(heading)
        source = context.source
        video = source.facts['video']
        origin = f'Twitch clip: {source.clip.id}' if source.clip else 'Local shared video'
        self.source_label = label(f'{origin}\nSaved source: {Path(source.asset.path).name}\n'
            f'File duration bound: {source.duration_us / 1_000_000:.6f} seconds · {video["width"]} × {video["height"]}')
        self.source_label.setToolTip(source.asset.path)
        self.source_label.setAccessibleName('Selected admitted source')
        layout.addWidget(self.source_label)
        fields = QFormLayout()
        self.title = QLineEdit(((source.clip.title if source.clip else Path(source.asset.path).stem) or 'Untitled Moment')[:512])
        self.title.setMaxLength(512)
        fields.addRow('Moment &title', self.title)
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, value in ((self.start,0), (self.end,source.duration_us / 1_000_000)):
            spin.setDecimals(6)
            spin.setRange(0, source.duration_us / 1_000_000)
            spin.setSingleStep(.1)
            spin.setValue(value)
            spin.setSuffix(' s')
        fields.addRow('&Start', self.start)
        fields.addRow('&End (exclusive)', self.end)
        layout.addLayout(fields)
        layout.addWidget(label('Choose the OBS scene shown in this source. The selected range stays within the saved file.'))
        self.selectors = QWidget()
        choices = QFormLayout(self.selectors)
        choices.setContentsMargins(0,0,0,0)
        self.kits = QComboBox()
        self.kits.setAccessibleName('Creator Kit')
        self.kits.currentIndexChanged.connect(self._kit_changed)
        self.more_kits = QPushButton('Load more Kits')
        self.more_kits.clicked.connect(self._more_kits)
        kit_row = QHBoxLayout()
        kit_row.addWidget(self.kits,1)
        kit_row.addWidget(self.more_kits)
        kit_label = QLabel('Creator &Kit')
        kit_label.setBuddy(self.kits)
        choices.addRow(kit_label, kit_row)
        self.versions = QComboBox()
        self.versions.setAccessibleName('Exact Creator Kit version')
        self.versions.currentIndexChanged.connect(self._version_changed)
        self.more_versions = QPushButton('Load more versions')
        self.more_versions.clicked.connect(self._more_versions)
        version_row = QHBoxLayout()
        version_row.addWidget(self.versions,1)
        version_row.addWidget(self.more_versions)
        version_label = QLabel('Kit &version')
        version_label.setBuddy(self.versions)
        choices.addRow(version_label, version_row)
        self.scenes = QComboBox()
        self.scenes.setAccessibleName('Exact OBS scene profile')
        self.scenes.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scenes.setMinimumContentsLength(24)
        choices.addRow('OBS &scene', self.scenes)
        layout.addWidget(self.selectors)
        self.profile_info = label('')
        layout.addWidget(self.profile_info)
        self.scenes.currentIndexChanged.connect(self._scene_changed)
        layout.addWidget(label('Create the Moment, then select Open edit. Review the framing and timing in the editor before exporting.'))
        layout.addStretch()
        self.message = label('')
        body.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText('Create Moment')
        self.save_button.setProperty('primary', True)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        body.addWidget(self.buttons)
        self._kits_loaded(context.kits)
        if self.kits.count():
            self._kit_changed()
        else:
            self.message.setText('Create a Creator Kit first, then prepare this saved source again.')
        self._controls()

    def _controls(self):
        self.selectors.setEnabled(not self.busy)
        self.save_button.setEnabled(not self.busy and self.choice is not None
                                    and not self.choice.refusal and self.scenes.currentIndex() >= 0)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(not self.busy)
        self.more_kits.setVisible(self.kit_cursor is not None)
        self.more_versions.setVisible(self.version_cursor is not None)
        self.title.setEnabled(not self.busy)
        self.start.setEnabled(not self.busy)
        self.end.setEnabled(not self.busy)

    def _read(self, operation, callback):
        if self.busy:
            return
        self.busy = True
        self.message.setText('Loading the selected Kit version…')
        self._controls()
        def finished(value):
            self.busy = False
            self.message.setText('')
            try:
                callback(value)
            except Exception as exc:
                self._failed(str(exc))
            self._controls()
        try:
            self.runner.run(operation, on_finished=finished, on_error=self._failed)
        except Exception as exc:
            self._failed(str(exc))

    def _kits_loaded(self, page):
        known = {self.kits.itemData(i)[0] for i in range(self.kits.count())}
        self.kits.blockSignals(True)
        for entry in page.entries:
            if entry.id not in known:
                self.kits.addItem(entry.columns[0], (entry.id,entry.revision))
        self.kits.blockSignals(False)
        self.kit_cursor = page.next_cursor

    def _more_kits(self):
        if self.kit_cursor is not None:
            after = self.kit_cursor
            self._read(lambda job:self.service.workspace.page('kits',after), self._kits_loaded)

    def _kit_changed(self, index=None):
        selected = self.kits.currentData()
        if self.busy or selected is None:
            return
        kit_id, version = selected
        self.choice = None
        self.version_names = {}
        self._read(lambda job:self.service.versions(kit_id,version),
                   lambda value:self._versions_loaded(value, version))

    def _versions_loaded(self, value, selected_version):
        page, selected = value
        for entry in page.entries:
            self.version_names[entry.revision] = entry.columns[0]
        self.version_names[selected.version] = selected.name
        self.version_cursor = page.next_cursor
        self.versions.blockSignals(True)
        self.versions.clear()
        for version in sorted(self.version_names, reverse=True):
            self.versions.addItem(f'{self.version_names[version]} · version {version}', version)
        for index in range(self.versions.count()):
            if self.versions.itemData(index) == selected_version:
                self.versions.setCurrentIndex(index)
                break
        self.versions.blockSignals(False)
        self._choice_loaded(selected)

    def _more_versions(self):
        if self.version_cursor is not None and self.choice is not None:
            kit_id, version, after = self.choice.kit_id, self.choice.version, self.version_cursor
            self._read(lambda job:self.service.versions(kit_id,version,after),
                       lambda value:self._versions_loaded(value,version))

    def _version_changed(self, index=None):
        selected = self.kits.currentData()
        version = self.versions.currentData()
        if not self.busy and selected is not None and version is not None:
            self.choice = None
            self._read(lambda job:self.service.kit(selected[0],version),self._choice_loaded)

    def _choice_loaded(self, choice):
        selected = self.scenes.currentData()
        self.choice = choice
        self.scenes.blockSignals(True)
        self.scenes.clear()
        scene_counts = Counter(scene for _,scene,_,_ in choice.profiles)
        for profile,scene,width,height in choice.profiles:
            text = f'{profile} · {scene}' if scene_counts[scene] > 1 else scene
            self.scenes.addItem(text,(profile,scene,width,height))
            self.scenes.setItemData(self.scenes.count()-1, f'{scene}\nProfile: {profile}', Qt.ItemDataRole.ToolTipRole)
        for index in range(self.scenes.count()):
            if self.scenes.itemData(index) == selected:
                self.scenes.setCurrentIndex(index)
                break
        self.scenes.blockSignals(False)
        self._scene_changed()
        self.message.setText(choice.refusal or ('' if choice.profiles else 'This Kit version needs an OBS scene profile. Configure it in Creator Kits.'))

    def _scene_changed(self, index=None):
        selected = self.scenes.currentData()
        self.profile_info.setText(f'Profile reference: {selected[2]} × {selected[3]} source pixels' if selected else '')

    def _save(self):
        if self.busy or self.choice is None or self.choice.refusal or self.scenes.currentData() is None:
            return
        if not self.title.text().strip():
            self.message.setText('Enter a Moment title.')
            self.title.setFocus()
            return
        start, end = round(self.start.value() * 1_000_000), round(self.end.value() * 1_000_000)
        if end <= start:
            self.message.setText('Choose an end after the start of the source range.')
            self.end.setFocus()
            return
        profile,scene,_,_ = self.scenes.currentData()
        choice = self.choice
        arguments = dict(expected_source_ticket=self.context.source.ticket,
            title=self.title.text(), source_in_us=start, source_out_us=end,
            kit_id=choice.kit_id, kit_version=choice.version, expected_kit_sha256=choice.descriptor_sha256,
            profile_id=profile, scene_key=scene)
        if self.context.source.clip:
            arguments['clip_id'] = self.context.source.clip.id
        else:
            arguments['asset_id'] = self.context.source.asset.id
        self.busy = True
        self.message.setText('Verifying the saved source and creating the Moment…')
        self._controls()
        try:
            self.runner.run(lambda job:self.service.create(**arguments),on_finished=self._created,on_error=self._failed)
        except Exception as exc:
            self._failed(str(exc))

    def _created(self, moment):
        self.created = moment
        self.busy = False
        self.accept()

    def _failed(self, error):
        self.busy = False
        self.message.setText(error)
        self._controls()

    def reject(self):
        if not self.busy:
            super().reject()

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            super().closeEvent(event)


class MomentPreparationBinding:
    def __init__(self, window, *, python=None):
        self.window = window
        self.service = SourceMomentPreparation(window.library, python=python)
        self.active = False
        self.dialog = None
        self.panel = QWidget(window)
        row = QHBoxLayout(self.panel)
        row.setContentsMargins(0,0,0,0)
        self.prepare = QPushButton('Prepare Moment')
        self.prepare.setProperty('primary', True)
        self.prepare.clicked.connect(self._prepare)
        row.addWidget(self.prepare)
        self.check = QPushButton('Check source')
        self.stop = QPushButton('Stop inspection')
        self.check.clicked.connect(lambda:self._settle(False))
        self.stop.clicked.connect(lambda:self._settle(True))
        row.addWidget(self.check);row.addWidget(self.stop)
        row.addStretch()
        window.content_layout.insertWidget(window.content_layout.indexOf(window.twitch)+1,self.panel)
        self._gate_changed()

    def _gate_changed(self):
        pending = self.service.has_unsettled
        self.panel.setVisible(self.window.kind in ('clips','assets') or pending)
        self.prepare.setEnabled(not self.active and not pending and self.window.action_allowed(self)
                                and self.window.kind in ('clips','assets') and self.window.selected_entry() is not None)
        for button in (self.check,self.stop):
            button.setVisible(pending);button.setEnabled(pending and not self.active)

    def _settle(self,stop):
        if self.active or not self.service.has_unsettled:return
        self.active=True;self._gate_changed()
        def finished(proof):
            self.active=False
            if not self.service.has_unsettled:self.window.release_action(self)
            self.window.message.setText('Source inspection settled. Prepare the selected source again.' if not self.service.has_unsettled else 'The original source inspection remains unresolved. Check or stop it again.')
            self._gate_changed()
        try:self.window.runner.run(lambda job:self.service.settle_local(stop=stop),on_finished=finished,on_error=self._failed)
        except Exception as exc:self._failed(str(exc))

    def _prepare(self):
        entry = self.window.selected_entry()
        if self.active or self.service.has_unsettled or self.window.kind not in ('clips','assets') or entry is None or not self.window.acquire_action(self):
            return
        self.active = True
        source_id, local = entry.id, self.window.kind == 'assets'
        self._gate_changed()
        self.window.message.setText('Inspecting local video… H.264 MP4, up to 180 seconds and 2 GiB; optional mono/stereo AAC.' if local else 'Checking the selected saved source and its intake result…')
        try:
            self.window.runner.run(lambda job:self.service.inspect_local(source_id) if local else self.service.inspect(source_id),on_finished=self._show,on_error=self._failed)
        except Exception as exc:
            self._failed(str(exc))

    def _show(self, context):
        self.dialog = MomentPreparationDialog(context,self.service,self.window.runner,self.window)
        self.dialog.finished.connect(self._finished)
        self.dialog.open()

    def _finished(self, result):
        dialog, self.dialog = self.dialog, None
        self.active = False
        self.window.release_action(self)
        if dialog.created is not None:
            self.window.message.setText('Moment saved with its selected source and Kit version. Select Open edit to continue.')
            self.window.load_view('moments',dialog.created.id)
        dialog.deleteLater()

    def _failed(self, error):
        self.active = False
        if not self.service.has_unsettled:self.window.release_action(self)
        self.window.message.setText(error)
        self._gate_changed()
