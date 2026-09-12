from __future__ import annotations

from article_reader.domain.language import DetectedScript
from article_reader.text.script import detect_script


def test_detects_latin_cyrillic_mixed_and_unknown() -> None:
    latin = detect_script("Članak sa srpskim latiničnim slovima i nemačkim umlautima: Größe.")
    cyrillic = detect_script(
        "Чланак са српским ћириличним словима: љ, њ, ђ, ћ, џ."  # noqa: RUF001
    )
    mixed = detect_script("Latinica " * 10 + "ћирилица " * 10)
    unknown = detect_script("1234 — !?")

    assert latin.script is DetectedScript.LATIN
    assert cyrillic.script is DetectedScript.CYRILLIC
    assert mixed.script is DetectedScript.MIXED
    assert unknown.script is DetectedScript.UNKNOWN
    assert latin.latin_letter_count > 0
    assert cyrillic.cyrillic_letter_count > 0


def test_small_foreign_script_quote_does_not_make_article_mixed() -> None:
    evidence = detect_script(
        "This long English article contains predominantly Latin text. " * 10 + "Цитат."
    )

    assert evidence.script is DetectedScript.LATIN
