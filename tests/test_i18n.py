"""i18n.tr: English is untouched, German translates the tooltip lines, unknown text passes through unchanged."""
import pytest

import i18n


@pytest.fixture(autouse=True)
def english_after_each_test():
    yield
    i18n.set_lang("en")


def test_english_text_is_returned_unchanged():
    i18n.set_lang("en")
    text = "5h usage 75% - running\npauses at 70%\nSERVER DOWN"
    assert i18n.tr(text) == text


def test_german_translates_tooltip_lines_one_by_one():
    i18n.set_lang("de")
    assert i18n.tr("5h usage 75% - running\npauses at 70%") == "5h-Nutzung 75% - läuft\npausiert bei 70%"


def test_german_keeps_text_it_does_not_know():
    i18n.set_lang("de")
    assert i18n.tr("Something the dashboard says for the first time") == "Something the dashboard says for the first time"


def test_non_text_values_pass_through():
    i18n.set_lang("de")
    assert i18n.tr(None) is None
    assert i18n.tr("") == ""


@pytest.mark.parametrize(("value", "german"), [("3h 59m", "3 Std 59 Min"), ("2d 1h", "2 T 1 Std")])
def test_durations_are_spelled_out_in_german(value, german):
    i18n.set_lang("de")
    assert i18n.tr(f"in {value}") == f"in {german}"
