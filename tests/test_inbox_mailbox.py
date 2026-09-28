"""Reading mail: parsing raw messages, and the IMAP client that fetches them.

The IMAP tests run against a fake server rather than a real one. That is not
only for speed: the agent's claim is that it never changes the mailbox, and a
fake is the only way to assert that nothing but SELECT ... readonly and
BODY.PEEK ever goes over the wire.
"""

from __future__ import annotations

import imaplib
from pathlib import Path

import pytest

from jobagent.config import Settings
from jobagent.inbox import mailbox as box
from jobagent.inbox.mailbox import (
    ImapMailbox,
    InboxNotConfigured,
    MailboxError,
    open_mailbox,
    parse_message,
)

EMAILS = Path(__file__).parent / "fixtures" / "emails"


def raw(name: str) -> bytes:
    return (EMAILS / f"{name}.eml").read_bytes()


def parsed(name: str):
    return parse_message(raw(name))


# ---------------------------------------------------------------- parsing --


def test_a_plain_text_message_comes_apart_into_its_pieces():
    message = parsed("acknowledgement")
    assert message.message_id == "ack-001@us.greenhouse-mail.io"
    assert message.subject == "Thank you for applying to Acme Robotics"
    assert message.from_name == "Acme Robotics"
    assert message.from_addr == "no-reply@us.greenhouse-mail.io"
    assert message.to_addr == "mark@example.com"
    assert message.received_at == "2026-09-21T09:14:02+00:00"
    assert "We have received your application" in message.body


def test_a_multipart_message_prefers_the_plain_part():
    message = parsed("interview")
    assert "invite you to an interview" in message.body
    assert "Ignore me" not in message.body


def test_an_html_only_message_loses_its_tags_scripts_and_styles():
    message = parsed("screening_html")
    assert "available" in message.body and "quick call" in message.body
    assert "<p>" not in message.body
    assert "alert(" not in message.body and "color: red" not in message.body


def test_a_message_with_no_id_gets_a_stable_one_of_its_own():
    first, second = parsed("no_message_id"), parsed("no_message_id")
    assert first.message_id.startswith("sha:")
    assert first.message_id == second.message_id, "the same mail must not be read twice"


def test_a_folded_subject_comes_back_on_one_line():
    assert parsed("no_message_id").subject == "Senior Backend Engineer at Acme Robotics"


def test_a_date_with_an_offset_is_stored_as_utc():
    assert parsed("no_message_id").received_at == "2026-09-22T16:00:00+00:00"


def test_a_message_with_no_date_is_stamped_with_now():
    message = parse_message(b"From: a@b.c\nSubject: hi\n\nbody")
    assert message.received_at.endswith("+00:00")


def test_the_sender_domain_is_the_part_after_the_at():
    assert parsed("interview").sender_domain == "acmerobotics.com"


def test_a_very_long_body_is_cut(monkeypatch):
    long_mail = b"Message-ID: <x@y>\nFrom: a@b.c\nSubject: hi\n\n" + (b"word " * 20_000)
    assert len(parse_message(long_mail).body) <= box.MAX_BODY_CHARS


def test_an_excerpt_is_one_line_and_short():
    excerpt = parsed("rejection").excerpt(limit=60)
    assert len(excerpt) == 60 and "\n" not in excerpt


# ------------------------------------------------------------------- dates --


@pytest.mark.parametrize(
    "given", ["2026-09-01", "2026-09-01T10:00:00", "2026-09-01T10:00:00+00:00", "2026-09-01Z"]
)
def test_imap_dates_are_accepted_in_the_shapes_we_store(given):
    assert box._imap_date(given.replace("Z", "T00:00:00Z")) == "01-Sep-2026"


def test_an_unreadable_date_says_so():
    with pytest.raises(MailboxError, match="cannot read"):
        box._imap_date("last tuesday")


def test_default_since_is_a_plain_date():
    assert len(box.default_since(30)) == len("2026-09-01")


# -------------------------------------------------------------- open_mailbox --


def test_an_unconfigured_mailbox_names_what_is_missing(tmp_path):
    with pytest.raises(InboxNotConfigured) as exc:
        open_mailbox(Settings(data_dir=tmp_path))
    assert "JOBAGENT_IMAP_HOST" in str(exc.value)
    assert "JOBAGENT_IMAP_PASSWORD" in str(exc.value)


def test_a_configured_mailbox_carries_the_settings(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        imap_host="imap.example.com",
        imap_user="mark@example.com",
        imap_password="secret",
        imap_folder="Applications",
    )
    made = open_mailbox(settings)
    assert isinstance(made, ImapMailbox)
    assert (made.host, made.folder, made.port) == ("imap.example.com", "Applications", 993)


# -------------------------------------------------------------------- IMAP --


class FakeIMAP:
    """Just enough of imaplib to prove what the client does and does not do."""

    def __init__(self, host, port=993):
        self.host, self.port = host, port
        self.calls: list[tuple] = []
        self.logged_out = False
        self.messages = {
            b"11": raw("acknowledgement"),
            b"12": raw("rejection"),
            b"13": raw("interview"),
        }
        self.search_status = "OK"

    error = imaplib.IMAP4.error

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"welcome"]

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"3"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return self.search_status, [b" ".join(self.messages)]
        wanted = args[0].encode()
        if wanted not in self.messages:
            return "NO", [None]
        return "OK", [(b"1 (BODY[] {10})", self.messages[wanted])]

    def logout(self):
        self.logged_out = True
        return "BYE", [b"bye"]


@pytest.fixture
def fake_imap(monkeypatch):
    made = {}

    def factory(host, port=993):
        made["conn"] = FakeIMAP(host, port)
        return made["conn"]

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    return made


def _mailbox() -> ImapMailbox:
    return ImapMailbox(host="imap.example.com", user="mark", password="secret")


def test_fetching_opens_the_folder_read_only_and_peeks_at_bodies(fake_imap):
    messages = _mailbox().fetch(since="2026-09-01")
    conn = fake_imap["conn"]

    assert [m.message_id for m in messages] == [
        "ack-001@us.greenhouse-mail.io",
        "rej-002@us.greenhouse-mail.io",
        "int-003@acmerobotics.com",
    ]
    assert ("select", "INBOX", True) in conn.calls
    assert ("uid", "SEARCH", None, "SINCE", "01-Sep-2026") in conn.calls
    fetches = [call for call in conn.calls if call[1] == "FETCH"]
    assert len(fetches) == 3 and all("(BODY.PEEK[])" in call for call in fetches)
    changes = {"STORE", "COPY", "MOVE", "EXPUNGE"}
    assert not any(c[1] in changes for c in conn.calls if c[0] == "uid"), "nothing is changed"
    assert conn.logged_out


def test_without_a_since_every_message_is_searched(fake_imap):
    _mailbox().fetch()
    assert ("uid", "SEARCH", None, "ALL") in fake_imap["conn"].calls


def test_a_limit_takes_the_newest_messages(fake_imap):
    messages = _mailbox().fetch(limit=2)
    assert [m.message_id for m in messages] == [
        "rej-002@us.greenhouse-mail.io",
        "int-003@acmerobotics.com",
    ]


def test_a_message_that_will_not_fetch_does_not_end_the_poll(fake_imap, monkeypatch):
    def factory(host, port=993):
        conn = FakeIMAP(host, port)
        original = conn.uid

        def uid(command, *args):
            if command == "FETCH" and args[0] == "13":
                return "NO", [None]
            return original(command, *args)

        conn.uid = uid
        return conn

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    messages = _mailbox().fetch()
    assert [m.message_id for m in messages] == [
        "ack-001@us.greenhouse-mail.io",
        "rej-002@us.greenhouse-mail.io",
    ]


def test_a_message_that_will_not_parse_does_not_end_the_poll(fake_imap, monkeypatch):
    real = box.parse_message

    def explode(data, **kwargs):
        if b"rej-002" in data:
            raise ValueError("this one is broken")
        return real(data, **kwargs)

    monkeypatch.setattr(box, "parse_message", explode)
    messages = _mailbox().fetch()
    assert [m.message_id for m in messages] == [
        "ack-001@us.greenhouse-mail.io",
        "int-003@acmerobotics.com",
    ]


def test_a_failed_search_is_reported(monkeypatch):
    def factory(host, port=993):
        conn = FakeIMAP(host, port)
        conn.search_status = "NO"
        return conn

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    with pytest.raises(MailboxError, match="search failed"):
        _mailbox().fetch()


def test_a_login_that_fails_says_which_host(monkeypatch):
    def factory(host, port=993):
        raise OSError("connection refused")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", factory)
    with pytest.raises(MailboxError, match="imap.example.com"):
        _mailbox().fetch()
