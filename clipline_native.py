"""Native Clipline entrypoint — Phase 0.

Replaces the Flask + QtWebEngine shell with a pure PySide6 app. The web stack
still lives on main for the duration of the native migration; this entrypoint
is what ``Clipline.spec`` builds and what the EXE launches.

Modes:

    python clipline_native.py
        Launch the GUI.

    python clipline_native.py --selftest <input.mp4> <output.mp4>
        Headless smoke test of the worker pipeline. No window opens; the
        clip is re-encoded through the same JobRunner the GUI uses. Exit 0
        on success, non-zero on failure. Used by CI release verification
        per ``ALERT_REBUILD_LESSONS.md`` §0/§2 — always verify the
        downloaded artifact, not local dist/.

    python clipline_native.py --selftest-deps
        Report tool discovery (ffmpeg / ffprobe / yt-dlp paths) and exit.
        Helpful for confirming a frozen build resolved bundled binaries
        correctly (ALERT §3).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Honor an offscreen platform plugin so selftest works on CI runners without
# a display. The GUI mode does not set this; it inherits the user's platform.
if "--selftest" in sys.argv or "--selftest-deps" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

# app.py defines all the path constants, tool discovery, and runtime helpers we
# need. Importing the module does not start Flask — only @app.route() handlers
# register on the in-process Flask object, which is never run. See
# native/MIGRATION_PLAN.md for the Phase 7 plan to delete the Flask layer.
import app as _legacy_app
from native.workers import JobRunner, ffmpeg_export


def _resolve_icon() -> Path:
    return _legacy_app.INTERNAL_DIR / "static" / "favicon.ico"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clipline", description="Clipline native shell")
    parser.add_argument(
        "--selftest",
        nargs=2,
        metavar=("INPUT", "OUTPUT"),
        help="Headlessly re-encode INPUT to OUTPUT via the worker pipeline and exit.",
    )
    parser.add_argument(
        "--selftest-deps",
        action="store_true",
        help="Print discovered tool paths and exit. Helpful inside frozen builds.",
    )
    return parser


def run_gui() -> int:
    """Launch the Qt main window. Returns the QApplication exit code."""
    app = QApplication.instance() or QApplication(sys.argv)
    runner = JobRunner()
    from native.ui.window import MainWindow

    window = MainWindow(
        runner=runner,
        ffmpeg=_legacy_app.FFMPEG or "ffmpeg",
        ffprobe=_legacy_app.FFPROBE or "ffprobe",
        output_dir=_legacy_app.get_output_dir(),
        icon_path=_resolve_icon(),
    )
    window.show()
    return app.exec()


def run_selftest(input_path: Path, output_path: Path) -> int:
    """Headless: run one export through the worker pipeline, exit 0 on success."""
    if not input_path.exists():
        print(f"FAIL: input not found: {input_path}", file=sys.stderr)
        return 2

    # JobRunner needs a Qt event loop to dispatch signals. We use a normal
    # QApplication on the offscreen platform plugin so signal dispatch works
    # without a display.
    qt_app = QApplication.instance() or QApplication(sys.argv)
    runner = JobRunner()

    state = {"result": None, "error": None, "last_pct": -1.0}

    def on_progress(msg: str) -> None:
        print(f"[progress] {msg}")

    def on_progress_pct(pct: float) -> None:
        # Print every 10% so CI logs stay readable.
        bucket = int(pct * 10)
        if bucket > int(state["last_pct"] * 10):
            state["last_pct"] = pct
            print(f"[progress] {int(pct * 100)}%")

    def on_finished(result: object) -> None:
        state["result"] = result
        qt_app.quit()

    def on_error(message: str) -> None:
        state["error"] = message
        qt_app.quit()

    runner.run(
        ffmpeg_export,
        _legacy_app.FFMPEG or "ffmpeg",
        _legacy_app.FFPROBE or "ffprobe",
        input_path,
        output_path,
        on_progress=on_progress,
        on_progress_pct=on_progress_pct,
        on_finished=on_finished,
        on_error=on_error,
    )
    qt_app.exec()
    runner.wait_all()

    if state["error"] is not None:
        print(f"FAIL: {state['error']}", file=sys.stderr)
        return 1
    if state["result"] is None or not Path(str(state["result"])).exists():
        print("FAIL: worker finished but output file is missing.", file=sys.stderr)
        return 1
    print(f"PASS: {state['result']}")
    return 0


def run_selftest_deps() -> int:
    """Print discovered tool paths. Used to verify a frozen build can find them."""
    rows = [
        ("ffmpeg", _legacy_app.FFMPEG),
        ("ffprobe", _legacy_app.FFPROBE),
        ("yt-dlp", _legacy_app.YTDLP),
    ]
    missing = [name for name, path in rows if not path]
    for name, path in rows:
        print(f"{name:<8} {path or '(not found)'}")
    return 1 if missing else 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.selftest_deps:
        return run_selftest_deps()
    if args.selftest:
        input_path = Path(args.selftest[0]).resolve()
        output_path = Path(args.selftest[1]).resolve()
        return run_selftest(input_path, output_path)
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
