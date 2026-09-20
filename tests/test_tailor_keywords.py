from __future__ import annotations

import pytest

from jobagent.tailor.keywords import TECH_LEXICON, coverage, posting_keywords, split_by_support
from jobagent.tailor.terms import STOPWORDS, numbers_in

# ----------------------------------------------------------------- lexicon --


def test_lexicon_is_normalised_and_survives_the_drop_rules():
    assert isinstance(TECH_LEXICON, frozenset)
    assert len(TECH_LEXICON) >= 200
    for term in TECH_LEXICON:
        assert term == term.strip().lower(), term
        assert len(term) >= 2, term
        assert term not in STOPWORDS, term
        assert term not in numbers_in(term), term
        assert "  " not in term, term


def test_lexicon_hits_including_multi_word_and_symbol_terms():
    text = (
        "we want power bi dashboards, a ci/cd pipeline, services in node.js and c++, "
        "and a little kubernetes."
    )
    found = posting_keywords(text)
    assert {"power bi", "ci/cd", "node.js", "c++", "kubernetes"} <= set(found)


def test_lexicon_matches_are_case_insensitive_whole_words():
    assert "java" in posting_keywords("Fluent in Java.")
    # "java" must not match inside "JavaScript", nor "sql" inside "PostgreSQL".
    found = posting_keywords("javascript and postgresql only")
    assert "java" not in found
    assert "sql" not in found
    assert {"javascript", "postgresql"} <= set(found)


# ------------------------------------------------------------ keyword_like --


def test_keyword_like_hits_capitalised_name_and_symbol_token():
    found = posting_keywords("Experience with Kubernetes is required, k8s in short.")
    assert "kubernetes" in found
    assert "k8s" in found


def test_keyword_like_source_catches_names_the_lexicon_lacks():
    found = posting_keywords("Familiarity with Zorbcloud and x9z tooling helps.")
    assert "zorbcloud" not in TECH_LEXICON
    assert {"zorbcloud", "x9z"} <= set(found)


def test_capitalised_first_word_of_a_sentence_is_not_a_name():
    text = "We hire carefully. Collaborate with designers! Mentor engineers? Zorbcloud is used."
    found = posting_keywords(text)
    assert "collaborate" not in found
    assert "mentor" not in found
    assert "zorbcloud" not in found


def test_line_breaks_start_sentences_so_bullet_verbs_are_not_names():
    text = "What you will do\n- Collaborate with designers\n- Mentor engineers using Zorbcloud\n"
    found = posting_keywords(text)
    assert "collaborate" not in found
    assert "mentor" not in found
    assert "zorbcloud" in found


def test_abbreviations_do_not_end_a_sentence():
    found = posting_keywords("Any Python web framework, e.g. Zorbcloud, is fine.")
    assert "zorbcloud" in found
    assert "e.g" not in found


# ------------------------------------------------------------------- extra --


def test_extra_terms_count_only_when_present_in_the_posting():
    text = "You will run warehouse robotics deployments."
    found = posting_keywords(text, extra=["warehouse robotics", "zorbcloud"])
    assert "warehouse robotics" in found
    assert "zorbcloud" not in found


def test_extra_terms_are_normalised_and_blank_ones_ignored():
    extra = ["  Warehouse  Robotics ", "", " "]
    found = posting_keywords("Warehouse Robotics at scale", extra=extra)
    assert "warehouse robotics" in found
    assert "warehouse  robotics" not in found


def test_extra_term_survives_even_as_part_of_a_longer_lexicon_term():
    found = posting_keywords("Reports live in Google Analytics.", extra=["analytics"])
    assert "google analytics" in found
    assert "analytics" in found


# ------------------------------------------------------------------ ordering --


def test_ordered_by_occurrences_then_first_appearance():
    text = "Rust first, then Python. Python again, and Python once more. Kafka last."
    assert posting_keywords(text) == ["python", "rust", "kafka"]


def test_title_counts_and_comes_first():
    found = posting_keywords("We need Python and Kafka. Python daily.", title="Kafka Engineer")
    assert found[:2] == ["kafka", "python"]


def test_first_appearance_breaks_ties_regardless_of_alphabet():
    assert posting_keywords("redis and kafka") == ["redis", "kafka"]
    assert posting_keywords("kafka and redis") == ["kafka", "redis"]


def test_same_source_duplicates_and_cross_source_duplicates_collapse():
    text = "Kubernetes everywhere: kubernetes, KUBERNETES."
    found = posting_keywords(text, extra=["Kubernetes"])
    assert found == ["kubernetes"]


# --------------------------------------------------------------------- drops --


def test_exclude_drops_the_company_name_and_its_words():
    text = "Join Acme Robotics. We at Acme use Python, and Robotics is our world."
    with_company = posting_keywords(text)
    assert {"acme", "robotics"} <= set(with_company)
    found = posting_keywords(text, exclude=["Acme Robotics"])
    assert "acme" not in found
    assert "robotics" not in found
    assert "acme robotics" not in found
    assert "python" in found


def test_exclude_accepts_the_words_already_split():
    text = "Acme Robotics builds robots. Acme uses Python."
    found = posting_keywords(text, exclude=["acme robotics", "acme", "robotics"])
    assert found == ["python"]


def test_exclude_matches_whole_terms_only():
    found = posting_keywords("Google Analytics reporting at Google.", exclude=["Google"])
    assert "google" not in found
    assert "google analytics" in found


def test_fragment_of_a_present_multi_word_term_is_dropped():
    found = posting_keywords("Build Power BI reports. Power BI daily.")
    assert "power bi" in found
    assert "power" not in found
    assert "bi" not in found


def test_fragment_of_a_present_symbol_term_is_dropped():
    found = posting_keywords("Services on .NET Core and Node.js.")
    assert {".net", ".net core", "node.js"} <= set(found)
    assert "net" not in found
    assert "core" not in found


def test_fragment_rule_is_whole_word_not_substring():
    # "go" is a substring of "google analytics" but not a word of it.
    found = posting_keywords("Services in Go, tracked in Google Analytics.")
    assert "go" in found
    assert "google analytics" in found


def test_compound_of_present_terms_or_stopwords_is_dropped():
    found = posting_keywords("Python/Java and/or C++/C# and day-to-day TCP/IP work.")
    assert {"python", "java", "c++", "c#", "tcp/ip"} <= set(found)
    for glued in ("python/java", "and/or", "c++/c#", "day-to-day"):
        assert glued not in found


def test_pure_numbers_and_short_tokens_are_dropped():
    text = "Founded in 2024 with 10k users and 401k matching; 3+ years, R or C, v2.0 and s3."
    found = posting_keywords(text, extra=["r", "c", "3"])
    for dropped in ("2024", "10k", "401k", "3", "3+", "r", "c"):
        assert dropped not in found
    assert "v2.0" in found
    assert "s3" in found


def test_stopwords_are_dropped_from_every_source():
    found = posting_keywords("Lead the Team using Python.", extra=["lead", "team"])
    assert found == ["python"]


# ------------------------------------------------------------ empty and limit --


@pytest.mark.parametrize(
    ("description", "title"),
    [(None, None), ("", None), ("   \n", ""), (None, "   ")],
)
def test_empty_inputs_give_no_keywords(description, title):
    assert posting_keywords(description, title) == []


def test_title_alone_is_enough():
    assert posting_keywords(None, "Python Developer") == ["python"]


def test_limit_caps_after_ranking():
    text = "Rust, Python, Python, Kafka, Kafka, Kafka."
    assert posting_keywords(text, limit=2) == ["kafka", "python"]
    assert posting_keywords(text, limit=0) == []
    assert len(posting_keywords(text, limit=100)) == 3


def test_negative_limit_is_rejected():
    with pytest.raises(ValueError):
        posting_keywords("Python", limit=-1)


def test_results_are_distinct_lowercase_strings():
    text = "Python, PYTHON, Kubernetes, k8s, Power BI, Zorbcloud."
    found = posting_keywords(text)
    assert len(found) == len(set(found))
    assert all(term == term.lower() for term in found)


# ---------------------------------------------------------------- coverage --


def test_coverage_arithmetic_and_rounding():
    text = "I know Python and Django, deployed on Kubernetes."
    assert coverage(text, ["python", "django", "kafka"]) == 0.667
    assert coverage(text, ["python", "kafka", "redis"]) == 0.333
    assert coverage(text, ["python", "django", "kubernetes"]) == 1.0
    assert coverage(text, ["kafka"]) == 0.0


def test_coverage_of_empty_keywords_or_empty_text_is_zero():
    assert coverage("Python", []) == 0.0
    assert coverage("Python", ["", "  "]) == 0.0
    assert coverage("", ["python"]) == 0.0


def test_coverage_uses_whole_word_case_insensitive_matching():
    assert coverage("Google Cloud", ["go"]) == 0.0
    assert coverage("services in GO", ["go"]) == 1.0
    assert coverage("Power  BI and C++", ["power bi", "c++"]) == 1.0
    assert coverage("C++", ["c"]) == 0.0


def test_coverage_counts_blank_keywords_out_of_the_denominator():
    assert coverage("Python", ["python", ""]) == 1.0


# -------------------------------------------------------- split_by_support --


def test_split_by_support_preserves_order():
    keywords = ["kafka", "python", "power bi", "django", "zorbcloud"]
    pool = "Python and Django services; Power BI reporting."
    supported, missing = split_by_support(keywords, pool)
    assert supported == ["python", "power bi", "django"]
    assert missing == ["kafka", "zorbcloud"]


def test_split_by_support_with_nothing_to_split_or_no_pool():
    assert split_by_support([], "Python") == ([], [])
    assert split_by_support(["python"], "") == ([], ["python"])
