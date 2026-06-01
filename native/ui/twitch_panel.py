"""Twitch connect + VOD/clip browser, embedded in the Ingest stage.

The auth-first ingest base: connect once (device-code flow), then pick a VOD or
clip from your own channel and it downloads straight into the preview. Manual
URL paste still works alongside this — both end at the same ``on_ingest`` call.

The panel is self-contained: it drives ``twitch_auth`` / ``twitch_api`` and runs
the (blocking) device-code poll + Helix fetches on the shared ``JobRunner`` so
the UI never freezes. Picking an item just hands a URL to ``on_ingest`` — the
window owns the actual yt-dlp download.
"""
from __future__ import annotations

import threading
import webbrowser
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.services import twitch_api, twitch_auth
from native.ui import theme
from native.workers import JobRunner


class TwitchPanel(QFrame):
    def __init__(self, runner: JobRunner, on_ingest: Callable[[str], None]) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._runner = runner
        self._on_ingest = on_ingest
        self._cancel: Optional[threading.Event] = None
        self._user: dict = {}

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(20, 18, 20, 18)
        self._layout.setSpacing(10)
        self._render()

    # ── rendering ───────────────────────────────────────────────────

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                TwitchPanel._clear_layout(item.layout())

    def _kicker(self, text: str) -> None:
        k = QLabel(text)
        k.setProperty("kicker", True)
        self._layout.addWidget(k)

    def _render(self) -> None:
        self._clear()
        self._kicker("TWITCH")
        if not twitch_auth.client_id():
            self._render_needs_client_id()
        elif twitch_auth.load_session():
            self._render_connected()
        else:
            self._render_disconnected()

    def _render_needs_client_id(self) -> None:
        msg = QLabel(
            "Connect your Twitch channel to pull your VODs and clips straight "
            "into the editor. First, set a Twitch application Client ID "
            "(register a free app at dev.twitch.tv → Applications, OAuth flow: "
            "Device, no secret needed)."
        )
        msg.setWordWrap(True)
        msg.setProperty("hint", True)
        self._layout.addWidget(msg)
        btn = QPushButton("Set Twitch Client ID…")
        btn.setProperty("primary", True)
        btn.clicked.connect(self._prompt_client_id)
        self._layout.addWidget(btn)

    def _render_disconnected(self) -> None:
        msg = QLabel("Connect to list your past broadcasts and clips.")
        msg.setWordWrap(True)
        msg.setProperty("hint", True)
        self._layout.addWidget(msg)
        row = QHBoxLayout()
        connect = QPushButton("Connect Twitch")
        connect.setProperty("primary", True)
        connect.clicked.connect(self._start_connect)
        row.addWidget(connect)
        change = QPushButton("Change ID")
        change.clicked.connect(self._prompt_client_id)
        row.addWidget(change)
        row.addStretch(1)
        self._layout.addLayout(row)

    def _render_connecting_browser(self) -> None:
        self._clear()
        self._kicker("TWITCH")
        info = QLabel(
            "A Twitch tab opened in your browser — click <b>Authorize</b> to "
            "connect. This window will update automatically."
        )
        info.setWordWrap(True)
        info.setProperty("hint", True)
        self._layout.addWidget(info)
        row = QHBoxLayout()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel_connect)
        row.addWidget(cancel)
        row.addStretch(1)
        self._layout.addLayout(row)

    def _render_connecting(self, code: str, uri: str) -> None:
        self._clear()
        self._kicker("TWITCH")
        info = QLabel(
            f"In the browser window, enter this code to authorize Clipline:"
        )
        info.setWordWrap(True)
        info.setProperty("hint", True)
        self._layout.addWidget(info)
        code_label = QLabel(code)
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_label.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 30px; font-weight: 700; "
            f"letter-spacing: 4px; padding: 8px;"
        )
        self._layout.addWidget(code_label)
        row = QHBoxLayout()
        reopen = QPushButton("Reopen browser")
        reopen.clicked.connect(lambda: webbrowser.open(uri))
        row.addWidget(reopen)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._cancel_connect)
        row.addWidget(cancel)
        row.addStretch(1)
        self._layout.addLayout(row)

    def _render_connected(self) -> None:
        session = twitch_auth.load_session() or {}
        who = self._user.get("display_name") or session.get("login") or "your channel"
        head = QHBoxLayout()
        label = QLabel(f"Connected as {who}")
        label.setStyleSheet(f"color: {theme.INK_BRIGHT}; font-weight: 600;")
        head.addWidget(label)
        head.addStretch(1)
        disconnect = QPushButton("Disconnect")
        disconnect.clicked.connect(self._disconnect)
        head.addWidget(disconnect)
        self._layout.addLayout(head)

        tabs = QHBoxLayout()
        vods_btn = QPushButton("VODs")
        vods_btn.clicked.connect(lambda: self._fetch("vods"))
        clips_btn = QPushButton("Clips")
        clips_btn.clicked.connect(lambda: self._fetch("clips"))
        tabs.addWidget(vods_btn)
        tabs.addWidget(clips_btn)
        tabs.addStretch(1)
        self._layout.addLayout(tabs)

        self._list = QListWidget()
        self._list.setMinimumHeight(160)
        self._list.itemActivated.connect(self._on_item_activated)
        self._layout.addWidget(self._list)

        self._status = QLabel("Pick VODs or Clips to load your library.")
        self._status.setProperty("hint", True)
        self._status.setWordWrap(True)
        self._layout.addWidget(self._status)

        # If we already know the user, prefetch VODs; otherwise resolve identity.
        if self._user:
            self._fetch("vods")
        else:
            self._resolve_user_then_fetch()

    # ── client id ─────────────────────────────────────────────────────

    def _prompt_client_id(self) -> None:
        current = twitch_auth.client_id()
        value, ok = QInputDialog.getText(
            self, "Twitch Client ID",
            "Paste your Twitch application Client ID:", text=current,
        )
        if ok and value.strip():
            twitch_auth.set_client_id(value.strip())
            self._render()

    # ── connect flow ──────────────────────────────────────────────────

    def _start_connect(self) -> None:
        # Primary: loopback "Sign in with Twitch" — click Authorize, bounce back.
        self._render_connecting_browser()
        self._cancel = threading.Event()
        self._runner.run(
            twitch_auth.loopback_login,
            self._cancel,
            on_finished=self._on_connected,
            on_error=self._on_connect_error,
        )

    def _start_device_connect(self) -> None:
        # Fallback when the loopback port is busy: device-code flow.
        try:
            device = twitch_auth.request_device_code()
        except Exception as exc:
            QMessageBox.warning(self, "Twitch", f"Could not start login:\n{exc}")
            self._render()
            return
        webbrowser.open(device.verification_uri)
        self._render_connecting(device.user_code, device.verification_uri)
        self._cancel = threading.Event()
        self._runner.run(
            twitch_auth.poll_token,
            device,
            self._cancel,
            on_finished=self._on_connected,
            on_error=self._on_connect_error,
        )

    def _cancel_connect(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        self._render()

    def _on_connected(self, _session: object) -> None:
        self._cancel = None
        self._user = {}
        self._render()

    def _on_connect_error(self, message: str) -> None:
        self._cancel = None
        if message == twitch_auth.PORT_BUSY_SENTINEL:
            # Loopback port busy — fall back to device-code flow automatically.
            self._start_device_connect()
            return
        if "cancelled" not in message.lower():
            QMessageBox.warning(self, "Twitch login", message)
        self._render()

    def _disconnect(self) -> None:
        twitch_auth.clear_session()
        self._user = {}
        self._render()

    # ── listing ───────────────────────────────────────────────────────

    def _resolve_user_then_fetch(self) -> None:
        self._status.setText("Loading your channel…")
        self._runner.run(
            lambda job: twitch_api.current_user(),
            on_finished=self._on_user_resolved,
            on_error=self._on_list_error,
        )

    def _on_user_resolved(self, user: object) -> None:
        self._user = user if isinstance(user, dict) else {}
        self._render()  # re-render now that we have the display name

    def _fetch(self, kind: str) -> None:
        if not self._user:
            self._resolve_user_then_fetch()
            return
        uid = self._user["id"]
        self._status.setText(f"Loading {kind}…")
        self._list.clear()
        if kind == "vods":
            fn = lambda job: twitch_api.list_vods(uid)
        else:
            fn = lambda job: twitch_api.list_clips(uid)
        self._runner.run(fn, on_finished=self._on_items, on_error=self._on_list_error)

    def _on_items(self, items: object) -> None:
        items = items or []
        self._list.clear()
        for it in items:
            label = f"{it.title}    ·  {it.duration or '?'}  ·  {it.kind.upper()}"
            entry = QListWidgetItem(label)
            entry.setData(Qt.ItemDataRole.UserRole, it.url)
            entry.setToolTip(it.url)
            self._list.addItem(entry)
        self._status.setText(
            f"{len(items)} item(s). Double-click to download into the editor."
            if items else "Nothing found on this channel yet."
        )

    def _on_list_error(self, message: str) -> None:
        if "reconnect" in message.lower() or "expired" in message.lower():
            twitch_auth.clear_session()
            self._user = {}
            self._render()
            QMessageBox.information(self, "Twitch", "Session expired — please reconnect.")
            return
        if hasattr(self, "_status"):
            self._status.setText(message)
        else:
            QMessageBox.warning(self, "Twitch", message)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        url = item.data(Qt.ItemDataRole.UserRole)
        if url:
            self._on_ingest(str(url))
