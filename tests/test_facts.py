from __future__ import annotations

import pytest

from jobagent.resume.facts import (
    Fact,
    add_fact,
    add_keywords,
    add_never_claim,
    list_facts,
    list_never_claim,
    remove_never_claim,
    set_active,
    violates_never_claim,
)


def test_round_trips_detail_and_tags(conn):
    fact_id = add_fact(
        conn,
        Fact(
            kind="role",
            text="Ran the payments migration",
            detail={"employer": "Acme", "start": "2021"},
            tags=["Python", "postgres", "python"],
        ),
    )
    stored = next(f for f in list_facts(conn) if f.id == fact_id)
    assert stored.detail["employer"] == "Acme"
    assert stored.tags == ["python", "postgres"], "tags are lowercased and deduplicated"


def test_rejects_an_unknown_kind(conn):
    with pytest.raises(ValueError, match="unknown fact kind"):
        add_fact(conn, Fact(kind="hobby", text="chess"))


def test_rejects_blank_text(conn):
    with pytest.raises(ValueError, match="needs text"):
        add_fact(conn, Fact(kind="skill", text="   "))


def test_user_keywords_are_first_class_facts(conn):
    add_keywords(conn, ["Terraform", "gRPC"])
    skills = list_facts(conn, kind="skill")
    assert {f.text for f in skills} == {"Terraform", "gRPC"}
    assert all(f.source == "user_added" for f in skills)


def test_keywords_are_not_duplicated(conn):
    add_keywords(conn, ["Terraform"])
    created = add_keywords(conn, ["terraform", "  ", "Kafka"])
    assert len(created) == 1
    assert len(list_facts(conn, kind="skill")) == 2


def test_deactivating_hides_a_fact_without_deleting_it(conn):
    fact_id = add_fact(conn, Fact(kind="skill", text="COBOL"))
    assert set_active(conn, fact_id, False) is True

    assert list_facts(conn) == []
    assert len(list_facts(conn, include_inactive=True)) == 1


def test_set_active_reports_a_missing_fact(conn):
    assert set_active(conn, 999, False) is False


def test_never_claim_is_case_insensitive(conn):
    add_never_claim(conn, "Kubernetes", note="never used it")
    assert violates_never_claim(conn, "Deployed services on kubernetes") == ["Kubernetes"]
    assert violates_never_claim(conn, "Deployed services on ECS") == []

    add_never_claim(conn, "kubernetes", note="still never used it")
    assert len(list_never_claim(conn)) == 1, "re-adding updates rather than duplicates"
    assert list_never_claim(conn)[0]["note"] == "still never used it"

    assert remove_never_claim(conn, "KUBERNETES") is True
    assert list_never_claim(conn) == []
