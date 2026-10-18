import ctypes
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from native.services.caption_worker.process_tree import ProcessTree


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object accounting")


class Accounting(ctypes.Structure):
    _fields_ = [("active_processes", ctypes.c_uint32)]


def tree_with_kernel(*, active=0, terminate=True, query=True):
    tree = object.__new__(ProcessTree)
    tree.process = SimpleNamespace(pid=123, poll=lambda: None)
    tree.scope_id = "fixture-scope"
    tree.job = 1
    tree.accounting = Accounting

    def inspect(job, kind, pointer, size, written):
        ctypes.cast(pointer, ctypes.POINTER(Accounting)).contents.active_processes = active
        return query

    tree.kernel = SimpleNamespace(TerminateJobObject=lambda *args: terminate,
                                  QueryInformationJobObject=inspect)
    return tree


def test_successful_termination_request_does_not_prove_empty_scope():
    tree = tree_with_kernel(active=1)
    with patch("native.services.caption_worker.process_tree.time.monotonic", side_effect=[0, 6]):
        result = tree.stop()
    assert result["terminate_requested"] is True
    assert result["disposition"] == "unknown"
    assert result["active_processes"] == 1


def test_failed_job_query_never_claims_stopped():
    result = tree_with_kernel(query=False).stop()
    assert result["disposition"] == "unknown"
    assert result["active_processes"] is None
    assert "query_error" in result


def test_empty_live_scope_is_verified_even_after_termination_failure():
    result = tree_with_kernel(terminate=False).stop()
    assert result["terminate_requested"] is False
    assert result["active_processes"] == 0
    assert result["disposition"] == "stopped"
    assert "terminate_error" in result
