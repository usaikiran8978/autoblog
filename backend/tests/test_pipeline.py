"""Unit tests for the pure logic — the parts worth testing without a network.

Deliberately focused on the deterministic core: URL normalization, the
originality check, ranking arithmetic, social trimming and SEO assembly.
Those are where a silent regression would actually publish something wrong.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.agents.collector import _social_score
from app.agents.deduplicator import _jaccard, _title_tokens
from app.agents.ranker import _recency
from app.agents.social import _fit, _normalize_tag
from app.agents.writer import _originality_score, _word_count
from app.services.embeddings import cosine_similarity, similarity_matrix
from app.services.fetchers.base import clean_text, normalize_url


# ------------------------------------------------------------ url handling
class TestNormalizeUrl:
    def test_strips_tracking_params(self):
        assert normalize_url(
            "https://Example.com/post?utm_source=x&utm_medium=y&id=42"
        ) == "https://example.com/post?id=42"

    def test_strips_fragment_and_trailing_slash(self):
        assert normalize_url("https://example.com/post/#section") == "https://example.com/post"

    def test_sorts_query_for_stable_hash(self):
        a = normalize_url("https://example.com/p?b=2&a=1")
        b = normalize_url("https://example.com/p?a=1&b=2")
        assert a == b, "query order must not change the dedupe hash"

    def test_lowercases_host_only(self):
        # Paths are case-sensitive on most servers; hosts are not.
        assert normalize_url("https://EXAMPLE.com/CaseSensitive") == (
            "https://example.com/CaseSensitive"
        )


class TestCleanText:
    def test_strips_html_and_collapses_whitespace(self):
        assert clean_text("<p>Hello   <b>world</b></p>\n\n") == "Hello world"

    def test_truncates_on_word_boundary(self):
        result = clean_text("alpha beta gamma delta epsilon", limit=14)
        assert result.endswith("…")
        assert "delt" not in result  # never cuts mid-word

    def test_empty_returns_none(self):
        assert clean_text("") is None
        assert clean_text(None) is None


# ------------------------------------------------------------------ dedupe
class TestDedupeHelpers:
    def test_title_tokens_drop_stopwords(self):
        tokens = _title_tokens("The New OpenAI Model Is Here")
        assert "the" not in tokens and "is" not in tokens
        assert {"openai", "model"} <= tokens

    def test_jaccard_identical(self):
        t = _title_tokens("OpenAI ships GPT-5 with longer context")
        assert _jaccard(t, t) == 1.0

    def test_jaccard_syndicated_headline(self):
        a = _title_tokens("OpenAI launches new reasoning model")
        b = _title_tokens("OpenAI launches new reasoning model for developers")
        assert _jaccard(a, b) > 0.7, "syndicated copies should be caught lexically"

    def test_jaccard_unrelated(self):
        a = _title_tokens("OpenAI launches new model")
        b = _title_tokens("Kubernetes 1.32 improves scheduler performance")
        assert _jaccard(a, b) < 0.15

    def test_empty_sets_are_not_similar(self):
        assert _jaccard(set(), set()) == 0.0


class TestSimilarity:
    def test_cosine_identical_is_one(self):
        assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_cosine_orthogonal_is_zero(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_does_not_divide_by_zero(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_matrix_diagonal_is_one(self):
        matrix = similarity_matrix([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
        assert matrix.shape == (3, 3)
        for i in range(3):
            assert matrix[i][i] == pytest.approx(1.0, abs=1e-5)

    def test_matrix_is_symmetric(self):
        matrix = similarity_matrix([[1.0, 2.0], [3.0, 1.0], [0.5, 0.5]])
        assert matrix[0][1] == pytest.approx(matrix[1][0], abs=1e-6)

    def test_empty_input(self):
        assert similarity_matrix([]).shape == (0, 0)


# ----------------------------------------------------------------- ranking
class TestRecency:
    def test_now_scores_near_one(self):
        assert _recency(datetime.now(timezone.utc)) > 0.99

    def test_twelve_hours_is_about_half(self):
        twelve = datetime.now(timezone.utc) - timedelta(hours=12)
        assert 0.45 < _recency(twelve) < 0.55

    def test_monotonic_decay(self):
        now = datetime.now(timezone.utc)
        scores = [_recency(now - timedelta(hours=h)) for h in (0, 6, 24, 72)]
        assert scores == sorted(scores, reverse=True)

    def test_naive_datetime_is_handled(self):
        # Feeds routinely emit naive timestamps; must not raise.
        assert 0.0 <= _recency(datetime.utcnow()) <= 1.0


class TestSocialScore:
    def test_no_popularity_is_zero(self):
        assert _social_score({}) == 0.0

    def test_unknown_platform_is_zero(self):
        assert _social_score({"platform": "mastodon", "score": 900}) == 0.0

    def test_log_scaling_compresses_high_end(self):
        low = _social_score({"platform": "hackernews", "points": 100})
        mid = _social_score({"platform": "hackernews", "points": 500})
        high = _social_score({"platform": "hackernews", "points": 2000})
        assert low < mid < high <= 1.0
        # The 100→500 jump should matter more than 500→2000.
        assert (mid - low) > (high - mid)


# ------------------------------------------------------------- originality
class TestOriginality:
    def test_verbatim_copy_scores_low(self):
        source = (
            "OpenAI announced a new model today that improves reasoning "
            "performance across a wide range of benchmark tasks and lowers cost."
        )
        originality, overlap = _originality_score(source, [source])
        assert originality < 0.2, "a verbatim copy must be flagged"
        assert overlap > 0.8

    def test_original_writing_scores_high(self):
        source = (
            "OpenAI announced a new model today that improves reasoning "
            "performance across a wide range of benchmark tasks."
        )
        article = (
            "The interesting part of this release is not the benchmark table. "
            "Pricing moved, and for anyone running inference at volume that "
            "changes the build-versus-buy math more than a few points of "
            "accuracy ever would."
        )
        originality, _ = _originality_score(article, [source])
        assert originality > 0.9

    def test_no_sources_is_fully_original(self):
        assert _originality_score("Some text here at all", [])[0] == 1.0

    def test_short_article_does_not_crash(self):
        assert _originality_score("too short", ["some source text"])[0] == 1.0

    def test_code_blocks_excluded(self):
        # Shared code samples are not plagiarism.
        source = "```python\nimport numpy as np\nx = np.array([1,2,3])\n```"
        originality, _ = _originality_score(source, [source])
        assert originality == 1.0


class TestWordCount:
    def test_ignores_markdown_syntax(self):
        assert _word_count("## Heading\n\n**bold** and *italic* text") == 5

    def test_excludes_code_blocks(self):
        md = "Real words here now.\n\n```python\nthis does not count at all\n```"
        assert _word_count(md) == 4


# ------------------------------------------------------------------ social
class TestSocialTrimming:
    def test_under_limit_is_untouched(self):
        assert _fit("short text", 100) == "short text"

    def test_prefers_sentence_boundary(self):
        text = "First sentence here. Second sentence that runs past the limit."
        result = _fit(text, 40)
        assert result == "First sentence here."

    def test_falls_back_to_word_boundary(self):
        result = _fit("a" * 10 + " " + "b" * 40, 25)
        assert not result.rstrip("…").endswith("b" * 40)
        assert len(result) <= 26

    def test_never_exceeds_limit_materially(self):
        long_text = "word " * 200
        assert len(_fit(long_text, 280)) <= 281

    def test_hashtag_normalization(self):
        assert _normalize_tag("machine learning") == "#machinelearning"
        assert _normalize_tag("#AI") == "#AI"
        assert _normalize_tag("  ") == ""


# --------------------------------------------------------------- seo/json-ld
class TestSEOHelpers:
    def test_truncate_respects_word_boundary(self):
        from app.agents.seo import _truncate

        result = _truncate("The quick brown fox jumps over the lazy dog", 20)
        assert len(result) <= 20
        assert not result.endswith("j")  # not mid-word

    def test_truncate_strips_dangling_punctuation(self):
        from app.agents.seo import _truncate

        assert not _truncate("Alpha beta, gamma delta", 12).endswith(",")


# ------------------------------------------------------------------ pricing
class TestPricing:
    def test_known_model_cost(self):
        from app.llm.pricing import estimate_cost

        # 1M in + 1M out on Opus 4.8 = $5 + $25
        assert estimate_cost(
            "claude", "claude-opus-4-8", input_tokens=1_000_000, output_tokens=1_000_000
        ) == pytest.approx(30.0)

    def test_cache_reads_are_cheaper_than_input(self):
        from app.llm.pricing import estimate_cost

        cached = estimate_cost("claude", "claude-opus-4-8", cache_read_tokens=1_000_000)
        fresh = estimate_cost("claude", "claude-opus-4-8", input_tokens=1_000_000)
        assert cached < fresh / 5

    def test_dated_snapshot_falls_back_to_base_model(self):
        from app.llm.pricing import price_for

        assert price_for("openai", "gpt-5.1-2025-11-13").input == price_for(
            "openai", "gpt-5.1"
        ).input

    def test_unknown_model_uses_fallback_not_zero(self):
        from app.llm.pricing import estimate_cost

        # Never silently report $0 for an unrecognised model.
        assert estimate_cost("openai", "totally-made-up", input_tokens=1_000_000) > 0


# ------------------------------------------------------------------ security
class TestSSRFGuard:
    def test_rejects_non_http_scheme(self):
        from app.core.security import assert_safe_url

        with pytest.raises(ValueError, match="scheme"):
            assert_safe_url("file:///etc/passwd")

    def test_rejects_blocked_port(self):
        from app.core.security import assert_safe_url

        with pytest.raises(ValueError, match="port"):
            assert_safe_url("http://example.com:6379/")

    def test_rejects_loopback(self):
        from app.core.security import assert_safe_url

        with pytest.raises(ValueError, match="non-public"):
            assert_safe_url("http://127.0.0.1/admin")

    def test_redact_does_not_leak_secret(self):
        from app.core.security import redact

        out = redact("sk-ant-supersecretvalue")
        assert "supersecret" not in out
        assert out.startswith("sk-a")
