"""Persistent library companion. Filmcraft remains the editing surface."""
import threading
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel
from PySide6.QtWidgets import (
    QAbstractItemView, QDateEdit, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLayout,
    QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QScrollArea, QTableView, QVBoxLayout, QWidget,
)

from native.services.workspace_library import LibraryPage, WorkspaceLibrary
from native.ui import theme
from native.workers import JobRunner


VIEWS = {
    'clips': ('Clips', ('Title', 'Created (UTC)', 'Duration', 'Views')),
    'moments': ('Moments', ('Title', 'Source', 'Creator Kit', 'Source range')),
    'kits': ('Creator Kits', ('Name', 'Latest version')),
    'versions': ('Kit versions', ('Name', 'Version')),
    'assets': ('Shared files', ('File', 'Size', 'Location')),
}
EMPTY = {
    'clips': 'Connect Twitch, then refresh clips for the selected dates. Saved clips stay here between sessions.',
    'moments': 'Your saved moments appear here with their source range and pinned Creator Kit version.',
    'kits': 'Saved Creator Kits appear here. Select a Kit to inspect its files and previous versions.',
    'versions': 'This Creator Kit has no saved versions.',
    'assets': 'Add a file once. Moments and Creator Kits reference the shared file without copying it.',
}


class LibraryModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.kind = 'clips'
        self.entries = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(VIEWS[self.kind][1])

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.columns[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return VIEWS[self.kind][1][section]
        return None

    def replace(self, page):
        self.beginResetModel()
        self.kind = page.kind
        self.entries = list(page.entries)
        self.endResetModel()

    def append(self, page):
        known = {(entry.id, entry.revision) for entry in self.entries}
        rows = [entry for entry in page.entries if (entry.id, entry.revision) not in known]
        if rows:
            start = len(self.entries)
            self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
            self.entries.extend(rows)
            self.endInsertRows()


class CreatorLibraryWindow(QMainWindow):
    def __init__(self, directory, *, runner=None, open_moment=None, parent=None):
        super().__init__(parent)
        self.library = WorkspaceLibrary(directory)
        self.runner = runner or JobRunner()
        self.open_moment = open_moment
        self.editor_available = True
        self.kind, self.context = 'clips', None
        self.next_cursor = None
        self._request = 0
        self._loading = False
        self._action_busy = False
        self._action_owner = None
        self._cancel = None
        self.setWindowTitle('Clipline · Creator Library')
        self.resize(1180, 780)
        self.setMinimumSize(760, 600)
        self.setStyleSheet(theme.GLOBAL_QSS + f'''
            QWidget {{ font-size: 16px; }}
            QPushButton[libraryNav="true"]:checked {{
                color: {theme.ACCENT}; border-color: {theme.ACCENT_DEEP};
                background-color: {theme.BG_HOVER};
            }}
        ''')
        font = self.font()
        font.setPixelSize(16)
        self.setFont(font)
        body = QWidget()
        body.setObjectName('stage')
        self.content = body
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(body)
        self.setCentralWidget(self.scroll)
        self.content_layout = layout = QVBoxLayout(body)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        brand = QLabel('Clipline')
        brand.setProperty('title', True)
        layout.addWidget(brand)
        nav = QHBoxLayout()
        self.nav = {}
        for key in ('clips', 'moments', 'kits', 'assets'):
            button = QPushButton(VIEWS[key][0])
            button.setCheckable(True)
            button.setProperty('libraryNav', True)
            button.clicked.connect(lambda checked=False, kind=key: self.load_view(kind))
            self.nav[key] = button
            nav.addWidget(button)
        nav.addStretch()
        layout.addLayout(nav)

        self.tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.textChanged.connect(self._filter)
        self.tools.addWidget(self.search, 1)
        self.reload = QPushButton('Reload')
        self.reload.clicked.connect(lambda: self.load_view(self.kind, self.context))
        self.tools.addWidget(self.reload)
        self.add = QPushButton('Add file…')
        self.add.setProperty('primary', True)
        self.add.clicked.connect(self._add_file)
        self.tools.addWidget(self.add)
        self.verify = QPushButton('Verify file')
        self.verify.clicked.connect(self._verify_file)
        self.tools.addWidget(self.verify)
        self.versions = QPushButton('View versions')
        self.versions.clicked.connect(self._versions)
        self.tools.addWidget(self.versions)
        self.open = QPushButton('Open edit')
        self.open.setProperty('primary', True)
        self.open.clicked.connect(self._open_moment)
        self.tools.addWidget(self.open)
        layout.addLayout(self.tools)

        self.twitch = QWidget()
        self.twitch_grid = QGridLayout(self.twitch)
        self.twitch_grid.setContentsMargins(0, 0, 0, 0)
        self.date_fields = QWidget()
        dates = QHBoxLayout(self.date_fields)
        dates.setContentsMargins(0, 0, 0, 0)
        self.from_date, self.to_date = QDateEdit(), QDateEdit()
        today = datetime.now(timezone.utc).date()
        for field, value in ((self.from_date, today - timedelta(days=7)), (self.to_date, today)):
            field.setCalendarPopup(True)
            field.setDisplayFormat('yyyy-MM-dd')
            field.setDate(value)
        for text, field in (('From', self.from_date), ('Through', self.to_date)):
            label = QLabel(text)
            label.setBuddy(field)
            dates.addWidget(label)
            dates.addWidget(field)
        dates.addWidget(QLabel('UTC'))
        dates.addStretch()
        self.auth_actions = QWidget()
        auth = QHBoxLayout(self.auth_actions)
        auth.setContentsMargins(0, 0, 0, 0)
        auth.addStretch()
        self.connect = QPushButton('Connect Twitch')
        self.connect.clicked.connect(self._connect_twitch)
        auth.addWidget(self.connect)
        self.refresh = QPushButton('Refresh clips')
        self.refresh.setProperty('primary', True)
        self.refresh.clicked.connect(self._refresh_clips)
        auth.addWidget(self.refresh)
        self.cancel = QPushButton('Cancel')
        self.cancel.clicked.connect(self._cancel_action)
        auth.addWidget(self.cancel)
        self.twitch_grid.addWidget(self.date_fields, 0, 0)
        self.twitch_grid.addWidget(self.auth_actions, 0, 1)
        self.twitch_grid.setColumnStretch(0, 1)
        layout.addWidget(self.twitch)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.hint)
        self.model = LibraryModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.table = QTableView()
        self.table.setMinimumHeight(100)
        self.table.setModel(self.proxy)
        self.table.setAccessibleName('Persistent creator library records')
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.selectionModel().selectionChanged.connect(self._selected)
        self.table.doubleClicked.connect(lambda index: self._open_moment())
        layout.addWidget(self.table, 1)
        footer = QHBoxLayout()
        self.count = QLabel()
        footer.addWidget(self.count)
        footer.addStretch()
        self.more = QPushButton('Load more')
        self.more.clicked.connect(self._more)
        footer.addWidget(self.more)
        layout.addLayout(footer)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName('Selected library record')
        self.details.setMaximumHeight(150)
        self.details.hide()
        layout.addWidget(self.details)
        self.message = QLabel('')
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        from native.ui.creator_kit_setup import KitSetupBinding
        self.kit_binding = KitSetupBinding(self)
        self.load_view('clips')

    def selected_entry(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        index = self.proxy.mapToSource(rows[0])
        return self.model.entries[index.row()]

    def action_allowed(self, owner):
        return self._action_owner is None or self._action_owner is owner

    def acquire_action(self, owner):
        if not self.action_allowed(owner):
            return False
        self._action_owner = owner
        self._action_busy = True
        self._controls()
        return True

    def release_action(self, owner):
        if self._action_owner is owner:
            self._action_owner = None
            self._action_busy = False
            self._controls()

    def _controls(self):
        for name in ('editor_binding', 'intake_binding', 'kit_binding'):
            binding = getattr(self, name, None)
            if binding is not None:
                binding._gate_changed()
        entry = self.selected_entry()
        self.twitch.setVisible(self.kind == 'clips')
        self.add.setVisible(self.kind == 'assets')
        self.add.setEnabled(not self._action_busy)
        self.verify.setVisible(self.kind == 'assets')
        self.verify.setEnabled(entry is not None and not self._action_busy)
        self.versions.setVisible(self.kind == 'kits')
        self.versions.setEnabled(entry is not None)
        self.open.setVisible(self.kind == 'moments' and self.open_moment is not None)
        self.open.setEnabled(entry is not None and self.editor_available and not self._action_busy)
        self.more.setVisible(self.next_cursor is not None)
        self.more.setEnabled(not self._loading)
        self.reload.setEnabled(not self._loading)
        for button in (self.connect, self.refresh):
            button.setEnabled(not self._action_busy)
        self.from_date.setEnabled(not self._action_busy)
        self.to_date.setEnabled(not self._action_busy)
        self.cancel.setVisible(self._cancel is not None)
        self.cancel.setEnabled(self._cancel is not None and not self._cancel.is_set())

    def load_view(self, kind, context=None):
        self.kind, self.context = kind, context
        self.next_cursor = None
        self._request += 1
        request = self._request
        self._loading = True
        self.search.clear()
        self.search.setPlaceholderText('Search loaded ' + VIEWS[kind][0].lower())
        self.search.setAccessibleName(self.search.placeholderText())
        self.details.clear()
        self.details.hide()
        self.model.replace(LibraryPage(kind, 0, None, (), context))
        self.hint.setText('Loading ' + VIEWS[kind][0].lower() + '…')
        self.hint.show()
        self.table.setEnabled(False)
        for key, button in self.nav.items():
            button.setChecked(key == kind or kind == 'versions' and key == 'kits')
        self._controls()
        self.runner.run(
            lambda job: self.library.page(kind, context=context),
            on_finished=lambda page: self._page(page, request),
            on_error=lambda error: self._page_error(error, request),
        )

    def _page(self, page, request):
        if request != self._request:
            return
        if page.after == 0:
            self.model.replace(page)
            header = self.table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            if page.kind == 'assets':
                header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            if page.kind == 'moments':
                for column, width in ((1, 200), (2, 220)):
                    header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                    self.table.setColumnWidth(column, width)
        else:
            self.model.append(page)
        self.next_cursor = page.next_cursor
        self._loading = False
        self.table.setEnabled(True)
        self.hint.setText(EMPTY[self.kind] if not self.model.entries else '')
        self.hint.setVisible(not self.model.entries)
        self._filter(self.search.text())
        self._controls()

    def _page_error(self, error, request):
        if request == self._request:
            self._loading = False
            self.hint.setText('The saved library could not be loaded. Reload to try again.')
            self.hint.show()
            self.message.setText(error)
            self._controls()

    def _more(self):
        if self.next_cursor is None or self._loading:
            return
        kind, cursor, context, request = self.kind, self.next_cursor, self.context, self._request
        self._loading = True
        self._controls()
        self.runner.run(
            lambda job: self.library.page(kind, cursor, context=context),
            on_finished=lambda page: self._page(page, request),
            on_error=lambda error: self._page_error(error, request),
        )

    def _filter(self, text):
        self.proxy.setFilterFixedString(text)
        self.count.setText(f'{self.proxy.rowCount()} shown · {self.model.rowCount()} loaded')

    def _selected(self):
        entry = self.selected_entry()
        self.details.setPlainText(entry.details if entry else '')
        self.details.setVisible(entry is not None)
        self._controls()
        if entry and self.kind in ('kits', 'versions'):
            request = self._request
            self.runner.run(
                lambda job: self.library.kit_details(entry.id, entry.revision),
                on_finished=lambda detail: self._kit_detail(detail, entry, request),
                on_error=lambda error: self._kit_detail(error, entry, request),
            )

    def _kit_detail(self, detail, entry, request):
        if request == self._request and self.selected_entry() == entry:
            self.details.setPlainText(detail)

    def _versions(self):
        entry = self.selected_entry()
        if entry and self.kind == 'kits':
            self.load_view('versions', entry.id)

    def _open_moment(self):
        entry = self.selected_entry()
        if (entry and self.kind == 'moments' and self.open_moment is not None
                and self.editor_available and not self._action_busy):
            self.open_moment(entry.id, entry.revision)

    def _action(self, fn, label, *, success='', cancellable=False):
        if self._action_busy or not self.acquire_action(self):
            return
        self._cancel = threading.Event() if cancellable else None
        self._action_success = success
        self.message.setText(label)
        self._controls()
        self.runner.run(fn, on_finished=self._action_finished, on_error=self._action_error)

    def _action_finished(self, result):
        self.release_action(self)
        self._cancel = None
        if isinstance(result, str):
            self.message.setText(result)
        elif hasattr(result, 'received'):
            state = 'Refresh cancelled' if result.cancelled else 'Refresh complete'
            self.message.setText(f'{state} · {result.received} clip records received. Saved clips remain available.')
        else:
            self.message.setText(self._action_success)
        self.load_view(self.kind, self.context)

    def _action_error(self, error):
        self.release_action(self)
        self._cancel = None
        self.message.setText(error)
        self.load_view(self.kind, self.context)

    def _add_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Add shared file')
        if path:
            self._action(lambda job: self.library.register_file(path), 'Checking and registering file…',
                         success='File registered in Shared files.')

    def _verify_file(self):
        entry = self.selected_entry()
        if entry:
            self._action(lambda job: self.library.verify_file(entry.id), 'Checking registered file…',
                         success='File matches its registered fingerprint.')

    def _connect_twitch(self):
        self._action(lambda job: self.library.connect_twitch(job, self._cancel),
                     'Complete Twitch authorization in your browser.', cancellable=True)

    def _refresh_clips(self):
        start = self.from_date.date().toPython().isoformat() + 'T00:00:00Z'
        end = (self.to_date.date().toPython() + timedelta(days=1)).isoformat() + 'T00:00:00Z'
        self._action(lambda job: self.library.refresh_clips(start, end, self._cancel),
                     'Refreshing channel clips…', cancellable=True)

    def _cancel_action(self):
        if self._cancel is not None:
            self._cancel.set()
            self.message.setText('Cancelling. Completed clip pages remain saved.')
            self._controls()

    def closeEvent(self, event):
        if self.runner._jobs or self._action_busy:
            self._cancel_action()
            self.message.setText('A library operation is finishing. Close this window after it finishes.')
            event.ignore()
        else:
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'auth_actions'):
            self.content_layout.setSpacing(8 if self.height() < 700 else 14)
            if hasattr(self, 'details'):
                self.details.setMaximumHeight(90 if self.height() < 700 else 150)
            self.twitch_grid.removeWidget(self.auth_actions)
            if self.width() < 1000:
                self.twitch_grid.addWidget(self.auth_actions, 1, 0, 1, 2)
            else:
                self.twitch_grid.addWidget(self.auth_actions, 0, 1)
