"""Delivery prompt variants for GPT-Live: v3 (default, v2 + behaving like a person), v2, v1 (older)."""
from __future__ import annotations

from prompts.live_prompt import LIVE_PROMPT, PROMPT_VARIANTS, build_live_prompt

COMMON = dict(language_name="українською", assistant_name=None, today="2026-10-09")


def test_variants():
    assert PROMPT_VARIANTS == ("v1", "v2", "v3", "v4", "v5")
    assert "you are talking, not reading" in build_live_prompt(**COMMON)
    assert "Being a person" in build_live_prompt(**COMMON)
    assert "Answering" in build_live_prompt(**COMMON)  # v4 by default
    assert "you are talking, not reading" not in build_live_prompt(variant="v1", **COMMON)
    assert "Listening like a person" in LIVE_PROMPT


def test_v3_adds_person_to_v2():
    v2 = build_live_prompt(variant="v2", **COMMON)
    v3 = build_live_prompt(variant="v3", **COMMON)
    assert "Being a person" not in v2 and "Being a person" in v3
    assert "you are talking, not reading" in v3  # v2's sound
    assert "end most replies with one short, specific question" in v3  # keeps the conversation going


def test_v4_answers_in_full():
    v3 = build_live_prompt(variant="v3", **COMMON)
    v4 = build_live_prompt(variant="v4", **COMMON)
    assert "Answering" in v4 and "Answering" not in v3
    # what kept her answers thin is gone from v4 only
    assert "Brief by default (one to three sentences)" in v3 and "Brief by default" not in v4
    assert "one good thought beats a full overview" in v3 and "one good thought" not in v4
    assert "end most replies with one short, specific question" not in v4
    assert "Answer first, then keep the talk going" in v4 and "never in place of the content" in v4
    assert "Listening like a person" not in v4 and "let them finish" in v4
    # the rest stays: sound, being a person, hearing, interruptions, delegation
    for part in ("you are talking, not reading", "Being a person", "Interruption policy", "Delegation:", "Pronunciation"):
        assert part in v4


def test_v4_balances_length():
    """Measured on GPT-Live (12 spoken lines, a judge on length): «Оттава. А ти там був?», a list of
    tips after «я втомився», two questions in a row — length right 7/12 → 20/24 with these rules."""
    from prompts.backend_prompt import BACKEND_PROMPT

    v4 = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="2026-10-14", variant="v4")
    assert "«Оттава.», not «Оттава. А ти там був?»" in v4 and "Never two questions in one reply" in v4
    assert "No advice, tips, ideas or plans they did not ask for" in v4
    assert "A quick fact or number: one sentence" in BACKEND_PROMPT and "no unasked advice" in BACKEND_PROMPT


def test_v5_is_v4_condensed():
    """v5: v4's rules said once each (v4 repeated «answer at once» three times, «one question at the
    end» twice) — under half the size, the rules that came from feedback still there."""
    v4 = build_live_prompt(variant="v4", **COMMON)
    v5 = build_live_prompt(variant="v5", voice="bossa", **COMMON)
    assert len(build_live_prompt(variant="v5", **COMMON)) < len(v4) / 2  # the rules; a character comes on top
    for part in ("Let them finish", "never answer a fragment", "«На Rust?»", "«Оттава.»",
                 "never «Хочеш, розкажу…?»", "no advice and no suggestions", "Never two.", "a made-up ability",
                 "Never dictate code", "rough figure", "always delegate it at once", "drop it and answer what "
                 "they said last", "new request — answer it", "Never assume their gender", "your character —"):
        assert part.lower() in v5.lower(), part
    assert "(prod)" not in v5


def test_v5_never_invents_recent_news():
    """Prod (Safari): «що цікавого сьогодні / вчора» got made-up news with no search («вчені знову
    обговорюють життя на Європі», «знайшли ще один фрагмент Леонардо»). Anything recent comes from a
    search; «Привіт, що нового?» stays small talk (it sent a greeting to the web)."""
    v5 = build_live_prompt(variant="v5", **COMMON)
    assert "You read and browse nothing on your own" in v5 and "«вчора читала»" in v5
    assert "what happened today, yesterday or lately in any field" in v5
    assert "is small talk too, not a request for the news" in v5


def test_examples_and_the_final_rule_follow_the_voice_gender():
    from prompts.live_prompt import LIVE_PROMPT, build_live_prompt

    assert "⟦" not in LIVE_PROMPT
    for variant in ("v1", "v2", "v3", "v4", "v5"):
        man = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="x", voice="meridian", variant=variant)
        woman = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="x", voice="willow", variant=variant)
        assert "⟦" not in man and "⟦" not in woman
        assert "Final rule: you are a man (Тарас)" in man and "Final rule: you are a woman (Єва)" in woman
        # Not one feminine example for a man to copy, and the other way round.
        assert not any(w in man for w in ("розчула", "Записала", "знайшла»"))
        assert "розчув" in man and "розчула" in woman and "розчув" not in woman


def test_every_character_but_the_cat_is_a_person():
    from prompts.live_prompt import build_live_prompt
    from voice.options import VOICE_PERSONAS

    for persona in VOICE_PERSONAS.values():
        text = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="x", voice=persona.id)
        assert persona.is_person == (persona.id != "kit")
        assert ("a living person, and you know it" in text) == persona.is_person
        if persona.is_person:  # the one limit: a sincere question out of the game is answered truthfully
            assert "never deny it" in text and "an AI playing" in text


def test_no_character_is_an_ai_assistant():
    """The strict rule closes every character's prompt (the cat too), and the sincere-question limit stays."""
    from prompts.live_prompt import build_live_prompt
    from voice.options import VOICE_PERSONAS

    for persona in VOICE_PERSONAS.values():
        text = build_live_prompt(language_name="Ukrainian", assistant_name=None, today="x", voice=persona.id)
        assert "STRICT RULE — who you are" in text and "NOT an AI assistant" in text or "You are NOT an AI" in text
        assert "never deny it" in text
