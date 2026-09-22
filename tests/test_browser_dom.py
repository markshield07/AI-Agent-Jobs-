"""Reading a form: what is on the page, what each control is, what it is called.

Everything above this layer works from `FormField`s, so what the reader makes
of a control decides what can be filled and what has to be asked. The fixture
carries each kind of control and each way a form labels one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.browser.dom import (
    clean_label,
    combobox_options,
    discover_fields,
    raw_controls,
)

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "controls.html"

pytestmark = pytest.mark.usefixtures("page")


@pytest.fixture
def fields(page):
    page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
    return {f.key: f for f in discover_fields(page)}


def test_a_label_is_found_however_the_form_writes_it(fields):
    assert fields["plain"].label == "Plain text"
    assert fields["wrapped"].label == "Wrapped"
    assert fields["sibling"].label == "Sibling label"
    assert fields["aria"].label == "By aria-label"
    assert fields["labelledby"].label == "By aria-labelledby"
    assert fields["placeheld"].label == "By placeholder"
    assert fields["nameless"].label == "", "no label rather than a wrong one"


def test_a_required_mark_is_not_part_of_the_question():
    assert clean_label("Full name*") == "Full name"
    assert clean_label("Full name ✱") == "Full name"
    assert clean_label("Full name (required)") == "Full name"
    assert clean_label("Full  name required ") == "Full name"
    assert clean_label("Rate us 1-5 *") == "Rate us 1-5"
    assert clean_label("") == ""


def test_each_control_gets_its_kind(fields):
    kinds = {key: field.kind for key, field in fields.items()}
    assert kinds["plain"] == "text"
    assert kinds["mail"] == "email"
    assert kinds["tel"] == "tel"
    assert kinds["years"] == "number"
    assert kinds["when"] == "date"
    assert kinds["story"] == "textarea"
    assert kinds["pick"] == "select"
    assert kinds["many"] == "multiselect"
    assert kinds["speed"] == "radio"
    assert kinds["top"] == "checkbox"
    assert kinds["agree"] == "checkbox"
    assert kinds["doc"] == "file"
    assert kinds["city"] == "select", "a combobox is a choice, not free text"


def test_options_come_with_the_field(fields):
    assert fields["pick"].options == ["Apples", "Bananas"], "a prompt is not an option"
    assert fields["many"].options == ["Red", "Green", "Blue"]
    assert fields["speed"].options == ["Slow", "Fast"]
    assert fields["top"].options == ["Cheese", "Olives"]


def test_a_group_is_one_question_with_its_legend(fields):
    assert fields["speed"].label == "Shipping speed"
    assert fields["top"].label == "Toppings"
    assert fields["speed"].selector == 'input[type="radio"][name="speed"]'


def test_a_lone_checkbox_asks_its_own_question(fields):
    """Not a group of one: its question is the box's own words. What that
    question means (a consent box, a marketing box) is the planner's call."""
    agree = fields["agree"]
    assert agree.label == "I agree to the terms"
    assert agree.required is True
    assert agree.options == [] and agree.kind == "checkbox"


def test_what_is_hidden_is_left_out_except_an_upload(fields):
    assert "gone" not in fields, "a hidden field is not on the form"
    assert "token" not in fields, "a hidden input is not a question"
    assert fields["doc"].kind == "file", "an upload behind its own button still counts"
    assert fields["doc"].accept == ".pdf"


def test_a_file_field_keeps_what_it_accepts_and_a_select_its_multiplicity(fields):
    assert fields["doc"].multiple is False
    assert fields["many"].multiple is True


def test_a_combobox_is_opened_to_read_its_options(page, fields):
    assert fields["city"].options == ["Austin, TX", "Boston, MA", "Denver, CO"]
    assert combobox_options(page, "#c-combo") == ["Austin, TX", "Boston, MA", "Denver, CO"]
    assert combobox_options(page, "#nothing-here") == []


def test_the_reader_can_be_asked_not_to_open_comboboxes(page):
    page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
    fields = {f.key: f for f in discover_fields(page, expand_comboboxes=False)}
    assert fields["city"].options == []
    assert fields["pick"].options == ["Apples", "Bananas"], "a real select needs no opening"


def test_fields_come_back_in_the_order_the_page_shows_them(page):
    page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
    keys = [f.key for f in discover_fields(page, expand_comboboxes=False)]
    assert keys.index("plain") < keys.index("mail") < keys.index("speed") < keys.index("city")
    assert len(keys) == len(set(keys)), "each control appears once"


def test_raw_controls_carries_the_page_detail(page):
    page.goto(FIXTURE.as_uri(), wait_until="domcontentloaded")
    controls = {c["name"]: c for c in raw_controls(page) if c.get("name")}
    assert controls["agree"]["required"] is True
    assert controls["doc"]["hidden"] is True, "the upload is hidden but kept"
    assert controls["plain"]["selector"] == "#c-text"
