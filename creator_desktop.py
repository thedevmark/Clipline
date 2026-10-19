"""Run the persistent Creator Library companion during the native rebuild."""
import argparse
from pathlib import Path
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from native.services.paths import APP_STATE_DIR, INTERNAL_DIR
from native.ui.creator_library import CreatorLibraryWindow


def main(argv=None):
    parser = argparse.ArgumentParser(description='Clipline Creator Library')
    parser.add_argument('--workspace', type=Path, default=APP_STATE_DIR / 'creator')
    parser.add_argument('--runtime-manifest', type=Path,
                        help='Explicit qualified local editor/helper identities for the native companion')
    parser.add_argument('--intake-runtime-manifest', type=Path,
                        help='Explicit qualified local source-download identities and public Twitch client ID')
    parser.add_argument('--caption-runtime-manifest',type=Path,action='append',help='Explicit local caption runtime; repeat for selectable profiles, with no automatic installation')
    parser.add_argument("--export-runtime-manifest",type=Path,help="Explicit qualified native dual-export identities")
    args = parser.parse_args(argv)
    editor_runtime = intake_runtime = caption_runtime = export_runtime = None
    # Reject malformed opt-in configuration before the window starts library workers.
    if args.runtime_manifest is not None:
        from native.services.editor_runtime import load_editor_runtime
        try:
            editor_runtime = load_editor_runtime(args.runtime_manifest)
        except Exception:
            parser.error('The editor runtime configuration could not be loaded.')
    if args.intake_runtime_manifest is not None:
        from native.services.intake_runtime import load_intake_runtime
        try:
            intake_runtime = load_intake_runtime(args.intake_runtime_manifest)
        except Exception:
            parser.error('The source-download runtime configuration could not be loaded.')
    if args.caption_runtime_manifest is not None:
        if editor_runtime is None:parser.error('Caption workflow requires the active native editor runtime.')
        from native.services.caption_runtime import load_caption_runtimes
        try:caption_runtime=load_caption_runtimes(args.caption_runtime_manifest)
        except Exception:parser.error('The caption runtime configuration could not be loaded.')
    if args.export_runtime_manifest is not None:
        if editor_runtime is None:parser.error("Export requires the active native editor runtime.")
        from native.services.export_runtime import load_export_runtime
        try:export_runtime=load_export_runtime(args.export_runtime_manifest)
        except Exception:parser.error("The export runtime configuration could not be loaded.")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Clipline')
    app.setWindowIcon(QIcon(str(INTERNAL_DIR / 'static' / 'favicon.ico')))
    window = CreatorLibraryWindow(args.workspace, probe_python=editor_runtime.python if editor_runtime else None)
    if editor_runtime is not None:
        from native.ui.workspace_editor import EditorCompanionBinding
        controller = editor_runtime.controller(window.library,caption_runtime=caption_runtime,export_runtime=export_runtime)
        window.editor_binding = EditorCompanionBinding(window, controller)
        from native.ui.workspace_creative import CreativeCompanionBinding
        window.editor_binding.creative_binding = CreativeCompanionBinding(window.editor_binding)
    if intake_runtime is not None:
        from native.services.workspace_intake import WorkspaceIntakeController
        from native.ui.workspace_intake import IntakeCompanionBinding
        window.intake_binding = IntakeCompanionBinding(window, WorkspaceIntakeController(window.library, intake_runtime))
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
