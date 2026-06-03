"""Native PySide6 shell for Clipline.

See native/MIGRATION_PLAN.md for the phased migration plan and
native/ALERT_REBUILD_LESSONS.md for the catch-up notes from the sibling
repo's rebuild. Phase 0 ships the skeleton; later phases fill in the
five stages (Project / Ingest / Inbox / Shorts / Output).
"""

# Single source of truth for the app version, surfaced in Help → About.
# Keep in step with the git release tag (CI builds on a pushed ``v*`` tag).
__version__ = "0.2.5"
