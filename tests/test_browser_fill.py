"""Putting a plan on a form, and reading what the page says back.

The filler is the only place that touches the page, so what it cannot do has
to come back as a reason rather than an exception: a control that is not
there, an option that does not exist, a file that is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.browser.dom import discover_fields
from jobagent.apply.browser.fill import (
    check_outcome,
    click_first_visible,
    detect_captcha,
    detect_login_wall,
    fill_field,
    fill_plan,
    mark_form,
    page_text,
    take_screenshot,
    visible_errors,
    wait_settled,
)
from jobagent.apply.models import Fill, FillPlan, FormField

FIXTURES = Path(__file__).parent / "fixtures" / "forms"
CONTROLS = FIXTURES / "controls.html"

pytestmark = pytest.mark.usefixtures("page")


@pytest.fixture
def form(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    return {f.key: f for f in discover_fields(page)}


def value(page, selector):
    return page.eval_on_selector(selector, "el => el.value")


# ------------------------------------------------------------ one field --


def test_text_boxes_take_their_value(page, form):
    assert fill_field(page, form["plain"], Fill(key="plain", value="Hello")) is None
    assert fill_field(page, form["mail"], Fill(key="mail", value="a@b.com")) is None
    assert fill_field(page, form["story"], Fill(key="story", value="Line one\nLine two")) is None
    assert value(page, "#c-text") == "Hello"
    assert value(page, "#c-email") == "a@b.com"
    assert value(page, "#c-area") == "Line one\nLine two"


def test_a_select_takes_an_option_by_its_text(page, form):
    assert fill_field(page, form["pick"], Fill(key="pick", value="Bananas")) is None
    assert value(page, "#c-select") == "b", "the option's value goes to the form"


def test_a_select_takes_a_near_enough_option(page, form):
    assert fill_field(page, form["pick"], Fill(key="pick", value="bananas")) is None
    assert value(page, "#c-select") == "b"


def test_an_option_that_does_not_exist_comes_back_as_a_reason(page, form):
    error = fill_field(page, form["pick"], Fill(key="pick", value="Cherries"))
    assert error and "cherries" in error.lower()
    assert value(page, "#c-select") == "", "nothing is picked rather than the wrong thing"


def test_several_options_go_on_a_multiselect(page, form):
    assert fill_field(page, form["many"], Fill(key="many", value=["Red", "Blue"])) is None
    picked = page.eval_on_selector(
        "#c-multi", "el => Array.from(el.selectedOptions).map(o => o.text)"
    )
    assert picked == ["Red", "Blue"]


def test_a_radio_group_takes_one_answer(page, form):
    assert fill_field(page, form["speed"], Fill(key="speed", value="Fast")) is None
    assert page.eval_on_selector('input[value="fast"]', "el => el.checked") is True
    assert page.eval_on_selector('input[value="slow"]', "el => el.checked") is False


def test_a_checkbox_group_takes_several(page, form):
    assert fill_field(page, form["top"], Fill(key="top", value=["Cheese", "Olives"])) is None
    checked = page.evaluate(
        "() => Array.from(document.querySelectorAll('input[name=top]'))"
        ".filter(b => b.checked).map(b => b.value)"
    )
    assert checked == ["Cheese", "Olives"]


def test_a_lone_checkbox_is_ticked_and_unticked_by_a_flag(page, form):
    assert fill_field(page, form["agree"], Fill(key="agree", value=True)) is None
    assert page.eval_on_selector('input[name="agree"]', "el => el.checked") is True
    assert fill_field(page, form["agree"], Fill(key="agree", value=False)) is None
    assert page.eval_on_selector('input[name="agree"]', "el => el.checked") is False


def test_an_upload_goes_to_a_hidden_file_input(page, form, tmp_path):
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-1.4 x")
    assert fill_field(page, form["doc"], Fill(key="doc", file_path=str(doc))) is None
    assert page.eval_on_selector("#file-hidden", "el => el.files[0].name") == "resume.pdf"


def test_a_missing_file_is_a_reason_not_a_crash(page, form, tmp_path):
    error = fill_field(page, form["doc"], Fill(key="doc", file_path=str(tmp_path / "nope.pdf")))
    assert error
    assert page.eval_on_selector("#file-hidden", "el => el.files.length") == 0


def test_a_combobox_is_opened_and_its_option_clicked(page, form):
    assert fill_field(page, form["city"], Fill(key="city", value="Boston, MA")) is None
    assert value(page, "#c-combo") == "Boston, MA"


def test_a_typeahead_takes_the_suggestion_its_prefix_brings_up(page, form):
    assert fill_field(page, form["city"], Fill(key="city", value="Denver")) is None
    assert value(page, "#c-combo") == "Denver, CO"


def test_a_control_that_is_not_there_is_a_reason(page, form):
    missing = FormField(key="ghost", label="Ghost", kind="text", selector="#not-here")
    error = fill_field(page, missing, Fill(key="ghost", value="x"))
    assert error


# -------------------------------------------------------------- a plan --


def test_a_plan_reports_what_went_on_and_what_would_not(page, form):
    plan = FillPlan(
        fills=[
            Fill(key="plain", value="Hello"),
            Fill(key="pick", value="Cherries"),
            Fill(key="story", value="Some words"),
            Fill(key="nowhere", value="x"),
        ]
    )
    form["pick"].required = True
    filled, needed, notes = fill_plan(page, list(form.values()), plan)

    assert [f.key for f in filled] == ["plain", "story"]
    assert [n.key for n in needed] == ["pick"], "a required field that would not take it is asked"
    assert needed[0].answer_key and "could not set it" in needed[0].reason
    assert any("nowhere" in note for note in notes), "a plan for a field the form lacks is a note"


def test_an_optional_field_that_would_not_take_it_is_only_a_note(page, form):
    plan = FillPlan(fills=[Fill(key="pick", value="Cherries")])
    filled, needed, notes = fill_plan(page, list(form.values()), plan)
    assert filled == [] and needed == []
    assert notes and "Pick one" in notes[0]


# ------------------------------------------------------- reading the page --


def test_the_form_that_was_filled_is_the_one_that_gets_pressed(page):
    page.goto((FIXTURES / "careers.html").as_uri(), wait_until="domcontentloaded")
    fields = discover_fields(page)
    application = [f for f in fields if f.key.startswith("applicant") or f.key == "cv"]

    scope = mark_form(page, application)
    assert scope
    assert page.eval_on_selector(scope, "el => el.id") == "apply"
    assert click_first_visible(page, [f"{scope} button[type=submit]"]) is True
    assert page.evaluate("() => window.__searched || null") is None, "the search was not run"

    assert mark_form(page, []) is None
    loose = FormField(key="x", label="x", kind="text", selector="#nothing")
    assert mark_form(page, [loose]) is None


def test_a_hidden_button_is_passed_over(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    page.evaluate(
        """() => {
            const decoy = document.createElement('button');
            decoy.type = 'submit'; decoy.id = 'decoy'; decoy.style.display = 'none';
            decoy.addEventListener('click', () => { window.__decoy = true; });
            document.getElementById('f').prepend(decoy);
        }"""
    )
    assert click_first_visible(page, ['button[type="submit"]']) is True
    assert page.evaluate("() => window.__decoy || false") is False
    assert click_first_visible(page, ["#nothing-here"]) is False


def test_a_confirmation_is_read_only_when_it_is_new(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    before_url, before_text = page.url, page_text(page)
    click_first_visible(page, ["#go"])
    wait_settled(page, 3000)
    outcome, text = check_outcome(page, before_url=before_url, before_text=before_text)
    assert outcome == "submitted" and "thank you for applying" in text.lower()

    again, _ = check_outcome(page, before_url=before_url, before_text=page_text(page))
    assert again == "unconfirmed", "what was already on the page is not a confirmation"


def test_an_error_after_the_button_is_a_failure(page):
    page.goto(CONTROLS.as_uri() + "?on_submit=error", wait_until="domcontentloaded")
    before_url, before_text = page.url, page_text(page)
    click_first_visible(page, ["#go"])
    wait_settled(page, 3000)
    outcome, text = check_outcome(page, before_url=before_url, before_text=before_text)
    assert outcome == "failed" and "email is not valid" in text
    assert visible_errors(page)


def test_a_page_that_says_nothing_is_unconfirmed(page):
    page.goto(CONTROLS.as_uri() + "?on_submit=nothing", wait_until="domcontentloaded")
    before_url, before_text = page.url, page_text(page)
    click_first_visible(page, ["#go"])
    wait_settled(page, 3000)
    outcome, text = check_outcome(page, before_url=before_url, before_text=before_text)
    assert outcome == "unconfirmed" and text is None


def test_a_url_that_says_it_worked_is_enough(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    outcome, text = check_outcome(
        page, before_url="https://x.example/apply", before_text=page_text(page)
    )
    assert outcome == "unconfirmed"
    page.goto(CONTROLS.as_uri() + "?submitted=1", wait_until="domcontentloaded")
    outcome, text = check_outcome(page, before_url="https://x.example/apply", before_text="")
    assert outcome == "submitted" and text


# ------------------------------------------------------------- the walls --


def test_a_captcha_is_named_when_one_is_on_the_page(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    assert detect_captcha(page) is None
    page.evaluate(
        """() => {
            const f = document.createElement('iframe');
            f.src = 'https://newassets.hcaptcha.com/captcha/v1/fixture#frame=challenge';
            f.setAttribute('srcdoc', '<p>pick the robots</p>');
            f.style.width = '300px'; f.style.height = '400px';
            document.body.appendChild(f);
        }"""
    )
    assert detect_captcha(page) == "hCaptcha"


def test_a_login_wall_is_a_password_box_with_no_form_behind_it(page):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    assert detect_login_wall(page) is False, "a form with an upload is not a login wall"
    page.evaluate("() => { document.body.innerHTML = '<input type=password>'; }")
    assert detect_login_wall(page) is True


def test_a_screenshot_is_written_and_a_missing_path_is_not_an_error(page, tmp_path):
    page.goto(CONTROLS.as_uri(), wait_until="domcontentloaded")
    shot = tmp_path / "deep" / "shot.png"
    assert take_screenshot(page, str(shot)) == str(shot)
    assert shot.is_file() and shot.stat().st_size > 0
    assert take_screenshot(page, None) is None
