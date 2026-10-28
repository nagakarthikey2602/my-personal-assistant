"""Unit tests for spoken-text normalisation and wake-word splitting."""

from __future__ import annotations

from personal_assistant.speech.voice_text import (
    clean_command,
    normalize_spoken_text,
    prepare_voice_command,
    split_wake_command,
    wake_aliases,
)


def test_normalize_spoken_punctuation_and_file_names() -> None:
    assert (
        normalize_spoken_text("create notes dot txt with content hello comma world")
        == "create notes.txt with content hello, world"
    )
    assert normalize_spoken_text("say hello period") == "say hello."
    assert normalize_spoken_text("open note pad and take a screen shot") == (
        "open notepad and take a screenshot"
    )


def test_wake_word_accepts_hearty_and_common_model_spellings() -> None:
    assert prepare_voice_command("hearty open notepad") == (True, "open notepad")
    assert prepare_voice_command("hardy open notepad") == (True, "open notepad")
    assert prepare_voice_command("harty open notepad") == (True, "open notepad")
    assert prepare_voice_command("heart y open notepad") == (True, "open notepad")


def test_wake_word_alone_returns_an_empty_command() -> None:
    assert prepare_voice_command("hearty") == (True, "")
    assert prepare_voice_command("hey hardy") == (True, "")


def test_polite_prefix_is_removed_after_wake_word() -> None:
    assert prepare_voice_command("hey hearty could you take a screenshot") == (
        True,
        "take a screenshot",
    )


def test_wake_word_must_be_at_the_start() -> None:
    heard, command = split_wake_command("please hearty open notepad")
    assert heard is False
    assert command == "please hearty open notepad"


def test_clean_command_removes_stacked_polite_prefixes() -> None:
    assert clean_command("Could you please open notepad") == "open notepad"
    assert clean_command("please kindly show my downloads") == "show my downloads"


def test_wake_aliases_are_unique_and_longest_first() -> None:
    aliases = wake_aliases("hearty", ["hardy", "HEARTY", "heart y", " "])
    assert aliases[0] == "heart y"
    assert aliases.count("hearty") == 1
    assert "hardy" in aliases


def test_default_wake_aliases_include_model_spellings() -> None:
    aliases = wake_aliases("hearty")
    assert "hardy" in aliases
    assert "heart y" in aliases
