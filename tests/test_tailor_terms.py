"""Term matching is the one place tailoring decides whether a claim is supported."""

from __future__ import annotations

import pytest

from jobagent.tailor.terms import contains_term, find_terms, keyword_like, numbers_in, tokens


@pytest.mark.parametrize(
    ("text", "term", "expected"),
    [
        ("We use C++ and Go.", "c++", True),
        ("We use C++ and Go.", "c", False),
        ("google cloud", "go", False),
        ("Go and Python", "go", True),
        ("Node.js services", "node.js", True),
        ("Kubernetes  cluster ops", "kubernetes cluster", True),
        ("c# code", "c#", True),
        ("c# code", "c", False),
        ("PostgreSQL", "postgres", False),
        ("", "python", False),
        ("python", "", False),
        ("python", "   ", False),
    ],
)
def test_contains_term(text, term, expected):
    assert contains_term(text, term) is expected


def test_find_terms_keeps_order_and_dedupes():
    wanted = ["python", "aws", "go", "Terraform"]
    assert find_terms("Python, AWS and Terraform; python again", wanted) == [
        "python",
        "aws",
        "terraform",
    ]
    assert find_terms("nothing", []) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Cut costs 40% ($1.2M) across 3,000 hosts in 10k days, 5+ years",
            {"40%", "$1.2m", "3000", "10k", "5+"},
        ),
        ("cut latency 40%.", {"40%"}),
        ("saving $1.2M.", {"$1.2m"}),
        ("since 2019.", {"2019"}),
        ("over 3,000.", {"3000"}),
        ("version v2 and node16", set()),
        ("", set()),
    ],
)
def test_numbers_in(text, expected):
    assert numbers_in(text) == expected


def test_tokens_keep_technology_symbols():
    assert tokens("Used C++, node.js and c# in 2019") == [
        "Used",
        "C++",
        "node.js",
        "and",
        "c#",
        "in",
        "2019",
    ]


def test_keyword_like_picks_names_not_prose():
    found = keyword_like(
        "Led migration to Kubernetes on AWS. Built CI/CD with GitHub Actions and node.js, "
        "c++ and k8s. The team grew."
    )
    assert {"kubernetes", "aws", "ci/cd", "github", "node.js", "c++", "k8s"} <= set(found)
    assert not {"led", "the", "built", "team", "migration"} & set(found)
    assert found.index("kubernetes") < found.index("aws")
    assert keyword_like("") == []
    assert keyword_like("Plain words only here.") == []


def test_keyword_like_resets_at_every_sentence():
    """A word that opens a sentence is not a name just because it is capitalised."""
    assert keyword_like("We build things. Cats are nice. Dogs too! Really? Yes.") == []
    assert keyword_like("We ship Rust. Then Go.") == ["rust", "go"]
    assert keyword_like("Tools, e.g. Zorb and Python.") == ["zorb", "python"]
