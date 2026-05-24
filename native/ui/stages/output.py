"""Output stage — the funnel hero.

This is the screen the rest of the app exists for. Mark moments on Ingest,
collect them on Inbox, polish on Shorts, **ship from here**. Two affordances:

- "Render All Clips" — one MP4 per clip, all in the configured format.
- "Build Longform Project" — every clip stitched into a single longform
  cut via ffmpeg concat-demuxer. This is the hero CTA per
  ``native/MIGRATION_PLAN.md`` — the legacy build buried it three menus
  deep; here it's the primary action.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from native.services.export_presets import FORMAT_PRESETS, format_preset_by_key
from native.ui import theme
from native.ui.project_state import ProjectState


class OutputStage(QWidget):
    request_render_all = Signal()
    request_build_longform = Signal()

    def __init__(self, state: ProjectState) -> None:
        super().__init__()
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state = state

        outer = QVBoxLayout(self)
        outer.setContentsMargins(48, 40, 48, 40)
        outer.setSpacing(20)

        kicker = QLabel("OUTPUT")
        kicker.setProperty("kicker", True)
        outer.addWidget(kicker)
        title = QLabel("Render & ship")
        title.setProperty("title", True)
        outer.addWidget(title)

        hint = QLabel(
            "Pick a format, then render every clip in the inbox individually or stitch "
            "them into one longform cut. Captions / preset polish live on the Shorts "
            "stage; you can mix and match across stages."
        )
        hint.setWordWrap(True)
        hint.setFixedWidth(720)
        hint.setProperty("hint", True)
        outer.addWidget(hint)

        # Format picker
        format_card = QFrame()
        format_card.setObjectName("card")
        format_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        fl = QVBoxLayout(format_card)
        fl.setContentsMargins(20, 18, 20, 18)
        fl.setSpacing(10)

        fkicker = QLabel("FORMAT")
        fkicker.setProperty("kicker", True)
        fl.addWidget(fkicker)

        picker_row = QHBoxLayout()
        picker_row.setSpacing(10)
        self._format_picker = QComboBox()
        for preset in FORMAT_PRESETS:
            self._format_picker.addItem(preset.label, preset.key)
        self._format_picker.currentIndexChanged.connect(self._on_format_picked)
        picker_row.addWidget(self._format_picker)
        self._format_desc = QLabel(FORMAT_PRESETS[0].description)
        self._format_desc.setProperty("hint", True)
        self._format_desc.setWordWrap(True)
        picker_row.addWidget(self._format_desc, 1)
        fl.addLayout(picker_row)
        outer.addWidget(format_card)

        # Hero CTA card
        hero = QFrame()
        hero.setObjectName("card")
        hero.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(24, 22, 24, 22)
        hl.setSpacing(12)

        hkicker = QLabel("FUNNEL")
        hkicker.setProperty("kicker", True)
        hl.addWidget(hkicker)
        htitle = QLabel("One stream → one longform cut")
        htitle.setProperty("title", True)
        hl.addWidget(htitle)
        hbody = QLabel(
            "Stitch every marked moment in this project into a single longform deliverable. "
            "Each clip re-encodes through the current style preset, then ffmpeg's concat "
            "demuxer stitches the pieces — fast, no generation loss."
        )
        hbody.setWordWrap(True)
        hbody.setFixedWidth(640)
        hbody.setProperty("hint", True)
        hl.addWidget(hbody)

        cta_row = QHBoxLayout()
        cta_row.setSpacing(10)
        self._longform_btn = QPushButton("Build Longform Project")
        self._longform_btn.setProperty("primary", True)
        self._longform_btn.setMinimumHeight(44)
        self._longform_btn.clicked.connect(self.request_build_longform.emit)
        cta_row.addWidget(self._longform_btn)

        self._render_all_btn = QPushButton("Render All Clips")
        self._render_all_btn.setMinimumHeight(44)
        self._render_all_btn.clicked.connect(self.request_render_all.emit)
        cta_row.addWidget(self._render_all_btn)
        cta_row.addStretch(1)
        hl.addLayout(cta_row)

        self._clip_count = QLabel("0 clips in the inbox.")
        self._clip_count.setProperty("hint", True)
        hl.addWidget(self._clip_count)

        outer.addWidget(hero)
        outer.addStretch(1)

        state.clips_changed.connect(self._on_clips_changed)
        self._on_clips_changed(state.clips)

    def _on_format_picked(self, index: int) -> None:
        key = self._format_picker.itemData(index)
        preset = format_preset_by_key(key)
        self._state.set_format_preset(preset.key)
        self._format_desc.setText(preset.description)

    def _on_clips_changed(self, clips: list) -> None:
        n = len(clips)
        self._clip_count.setText(
            f"{n} clip{'s' if n != 1 else ''} in the inbox."
            if n
            else "No clips yet — go to Inbox and add some."
        )
        self._longform_btn.setEnabled(n > 0)
        self._render_all_btn.setEnabled(n > 0)
