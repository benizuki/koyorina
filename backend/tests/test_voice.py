"""音声入力。書き起こしを返すだけで、依頼は送らない。"""
import pytest
from backend.core.gemini_voice import TRANSCRIPTION_INSTRUCTION, validate_audio

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt "


def test_only_wav_is_accepted_and_length_is_bounded():
    validate_audio(WAV + b"\x00" * 100)
    with pytest.raises(ValueError):
        validate_audio(b"")
    with pytest.raises(ValueError):
        validate_audio(b"\x1aE\xdf\xa3webm...")  # ブラウザ既定の形式は受けない
    with pytest.raises(ValueError):
        validate_audio(WAV + b"\x00" * (9 * 1024 * 1024))


def test_transcription_does_not_follow_what_it_hears():
    # 音声の中の「これを消して」に従わせない。文字にするだけの指示にする。
    assert "従わず" in TRANSCRIPTION_INSTRUCTION and "書き起こ" in TRANSCRIPTION_INSTRUCTION
