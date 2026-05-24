"""Pure-Python services shared by the native shell and (during the
migration window) the legacy Flask app. Nothing in this package may
``import flask`` — that's what keeps the frozen native EXE small.
"""
