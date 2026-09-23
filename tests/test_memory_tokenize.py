"""TDD (R3a): tokenizer — the reason Chinese recall used to return nothing.

Case 1 of docs/r3-analysis.md: ``_keywords_from("帮我给这个项目加一个依赖")`` produced
the whole sentence as a single keyword, so substring search never matched.  The
replacement splits CJK into character bigrams (which is what makes a query and a
note share terms), keeps identifiers such as ``config/pay.yaml`` intact, and drops
English stopwords (case 10: sharing only "the"/"file" used to inject anything).
"""

from ai_coding.memory.tokenize import normalize, similarity, strip_lead, tokenize


def test_chinese_splits_into_bigrams():
    tokens = tokenize("项目用uv管理依赖")
    assert "依赖" in tokens
    assert "项目" in tokens
    assert "项目用uv管理依赖" not in tokens  # never the whole sentence


def test_chinese_query_and_note_share_terms():
    query = set(tokenize("帮我给这个项目加一个依赖"))
    note = set(tokenize("这个项目用 uv 管理依赖，不要用 pip"))
    assert query & note  # the overlap that used to be empty


def test_short_cjk_segment_is_kept_whole():
    assert tokenize("依赖") == ["依赖"]


def test_chinese_particles_and_question_words_are_dropped():
    tokens = tokenize("依赖怎么装")
    assert "依赖" in tokens
    assert "怎么" not in tokens
    long_tokens = tokenize("这个项目加一个依赖")
    assert "项目" in long_tokens and "依赖" in long_tokens
    assert "这个" not in long_tokens


def test_english_drops_stopwords():
    tokens = tokenize("look at the file in the user module")
    assert "the" not in tokens and "at" not in tokens and "in" not in tokens
    assert "file" in tokens and "module" in tokens


def test_identifiers_survive():
    tokens = tokenize("see config/pay.yaml and todo_write() with RS256")
    assert "config/pay.yaml" in tokens
    assert "todo_write()" in tokens or "todo_write" in tokens
    assert "rs256" in tokens  # tokens are casefolded on purpose


def test_empty_and_punctuation_only_text():
    assert tokenize("") == []
    assert tokenize("，，！！？？") == []


def test_normalize_is_stable_across_case_space_and_punctuation():
    assert normalize("Use UV dependency") == normalize("use   uv  dependency")
    assert normalize("项目用 uv，管理依赖") == normalize("项目用uv管理依赖")


def test_normalize_strips_lead_ins():
    # prototype G2: "记住：" made an identical note look like a different one
    assert normalize("记住：项目用 uv 管理依赖") == normalize("项目用 uv 管理依赖")
    assert normalize("- 项目用 uv 管理依赖") == normalize("项目用 uv 管理依赖")
    assert normalize("note: project uses uv") == normalize("project uses uv")


def test_strip_lead_only_touches_the_prefix():
    assert strip_lead("记住：不要忘记跑测试") == "不要忘记跑测试"
    assert strip_lead("请记住：A 与 B") == "A 与 B"
    assert strip_lead("") == ""


def test_similarity_is_symmetric_and_bounded():
    a = "remember: the project uses uv for dependencies"
    b = "the project uses uv for dependencies"
    assert similarity(a, b) == similarity(b, a)
    assert 0.0 <= similarity(a, b) <= 1.0
    assert similarity(a, a) == 1.0
    assert similarity("", "abc") == 0.0


def test_similarity_merges_synonyms_with_a_lead_in():
    """Prototype G2: this pair must clear the 0.80 merge threshold."""
    assert similarity("记住：项目用 uv 管理依赖", "项目用 uv 管理依赖") >= 0.80
    assert similarity("项目用 uv 管理依赖", "项目改用 poetry 管理依赖") < 0.80
