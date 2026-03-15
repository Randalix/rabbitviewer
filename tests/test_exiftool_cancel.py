"""Tests for ExifToolProcess cancel_event support."""

import threading

import pytest

from plugins.exiftool_process import (
    ExifToolProcess, ExifToolCancelled, is_exiftool_available,
)

pytestmark = pytest.mark.skipif(
    not is_exiftool_available(),
    reason="exiftool not installed",
)


@pytest.fixture()
def et():
    proc = ExifToolProcess()
    yield proc
    proc.terminate()


class TestExifToolCancel:

    def test_cancel_event_raises(self, et):
        """A pre-set cancel_event raises ExifToolCancelled immediately."""
        evt = threading.Event()
        evt.set()

        with pytest.raises(ExifToolCancelled):
            et.execute(["-ver"], cancel_event=evt)

    def test_process_recovers_after_cancel(self, et):
        """After a cancel+restart, subsequent execute calls work."""
        evt = threading.Event()
        evt.set()  # already cancelled

        with pytest.raises(ExifToolCancelled):
            et.execute(["-ver"], cancel_event=evt)

        # Process was restarted — next call should work.
        result = et.execute(["-ver"])
        assert result  # exiftool version string

    def test_no_cancel_event_works_normally(self, et):
        """execute without cancel_event behaves as before."""
        result = et.execute(["-ver"])
        assert b"." in result  # version like "12.76"

    def test_cancel_does_not_corrupt_next_call(self, et):
        """After cancel, the next command returns clean output."""
        evt = threading.Event()
        evt.set()

        with pytest.raises(ExifToolCancelled):
            et.execute(["-ver"], cancel_event=evt)

        # Two consecutive calls after restart — both should return
        # clean version strings without stale sentinel data.
        v1 = et.execute(["-ver"])
        v2 = et.execute(["-ver"])
        assert v1 == v2
