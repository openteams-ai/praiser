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
