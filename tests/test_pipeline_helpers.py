from praiser.pipeline import _humanize


def test_humanize_seconds():
    assert _humanize(45) == "~45s"
    assert _humanize(89) == "~89s"


def test_humanize_minutes():
    assert _humanize(120) == "~2 min"
    assert _humanize(1800) == "~30 min"


def test_humanize_hours():
    assert _humanize(3600) == "~1h"
    assert _humanize(3900) == "~1h 5min"


def test_humanize_unknown():
    assert _humanize(None) == "shortly"
    assert _humanize(0) == "shortly"
