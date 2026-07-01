import io

from praiser.progress import Progress


def test_disabled_writes_nothing():
    buf = io.StringIO()
    p = Progress(enabled=False, stream=buf)
    p.phase("hello")
    p.status("working")
    p.done()
    assert buf.getvalue() == ""


def test_phase_writes_line():
    buf = io.StringIO()
    Progress(enabled=True, stream=buf).phase("discovering")
    assert buf.getvalue() == "[praiser] discovering\n"


def test_status_is_in_place_and_done_terminates():
    buf = io.StringIO()
    p = Progress(enabled=True, stream=buf)
    p.status("1/2")
    p.status("2/2")
    p.done()
    out = buf.getvalue()
    assert out.startswith("\r[praiser] 1/2")
    assert "\r[praiser] 2/2" in out
    assert out.endswith("\n")


def test_phase_clears_pending_status_line():
    buf = io.StringIO()
    p = Progress(enabled=True, stream=buf)
    p.status("scanning 5/10")
    p.phase("done")
    # The clear sequence blanks the previous line before the phase line.
    assert "\r" in buf.getvalue()
    assert buf.getvalue().endswith("[praiser] done\n")


def test_callback_fires_on_phase_and_status_even_when_display_disabled():
    # The web UI passes a callback with enabled=False (no terminal output).
    seen = []
    p = Progress(enabled=False, callback=seen.append)
    p.phase("discovering candidate repositories…")
    p.status("scanned 3/10 (1 found): numpy/numpy")
    assert seen == ["discovering candidate repositories…",
                    "scanned 3/10 (1 found): numpy/numpy"]


def test_callback_exception_never_breaks_progress():
    p = Progress(enabled=False, callback=lambda _m: 1 / 0)
    p.phase("safe")   # must not raise
    p.status("safe")  # must not raise
