#!/usr/bin/env python3
"""
Generate an Anki .apkg deck from a directory containing:
  - one media file (video or audio)
  - one English subtitle file named 'English.<ext>'
  - one foreign-language subtitle file named '<Language>.<ext>'

Front of each card:
  - still image thumbnail from the subtitle interval
  - English subtitle text

Back of each card:
  - foreign subtitle text
  - playable media clip for the interval (video for video input, audio for audio input)
  - optional generated TTS audio when the source media is in English

Dependencies:
  - ffmpeg / ffprobe
  - Python packages: genanki, pysubs2
  - Optional local TTS:
      * macOS 'say' command (default fallback)
      * sherpa-onnx-offline-tts with local model files

Example:
  python anki_video_deck.py /path/to/folder --deck-name "My Deck"
  python anki_video_deck.py /path/to/folder --deck-name "French Movie" --source-language english
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import genanki
except ImportError:  # pragma: no cover
    print("Missing dependency: genanki. Install with: pip install genanki", file=sys.stderr)
    raise

try:
    import pysubs2
except ImportError:  # pragma: no cover
    print("Missing dependency: pysubs2. Install with: pip install pysubs2", file=sys.stderr)
    raise

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    print("Missing dependency: pillow. Install with: pip install pillow", file=sys.stderr)
    raise


try:
    import sherpa_onnx
except ImportError as exc:  # pragma: no cover
    sherpa_onnx = None
    SHERPA_IMPORT_ERROR = exc
else:
    SHERPA_IMPORT_ERROR = None

try:
    import soundfile as sf
except ImportError as exc:  # pragma: no cover
    sf = None
    SOUNDFILE_IMPORT_ERROR = exc
else:
    SOUNDFILE_IMPORT_ERROR = None


VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma", ".alac", ".pcm", ".aiff"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | {".mp4"}  # .mp4 may be video or audio-only
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".ttml", ".smi", ".json", ".tmp", ".mpl2"}

LANGUAGE_ALIASES = {
    "english": "en",
    "spanish": "es",
    "espanol": "es",
    "español": "es",
    "italian": "it",
    "french": "fr",
    "german": "de",
    "persian": "fa",
    "farsi": "fa",
    "arabic": "ar",
    "mandarin": "zh",
    "chinese": "zh",
    "russian": "ru",
    "japanese": "ja",
    "tagalog": "tl",
    "filipino": "tl",
    "korean": "ko",
    "indonesian": "id",
}

# Reasonable defaults for macOS `say`. Users can override with --say-voice.
MACOS_SAY_VOICES = {
    "en": "Samantha",
    "es": "Jorge",
    "it": "Alice",
    "fr": "Thomas",
    "de": "Anna",
    "fa": None,
    "ar": None,
    "zh": "Tingting",
    "ru": "Milena",
    "ja": "Kyoko",
    "tl": None,
    "ko": "Yuna",
    "id": None,
}

SUPPORTED_LANGUAGES = {
    "es", "it", "fr", "de", "fa", "ar", "zh", "ru", "ja", "tl", "ko", "id"
}


def run(cmd: Sequence[str], *, capture_output: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        check=check,
        text=True,
        capture_output=capture_output,
    )


@dataclass
class SubtitleLine:
    start_ms: int
    end_ms: int
    text_raw: str
    text_plain: str

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass
class CardItem:
    idx: int
    start_ms: int
    end_ms: int
    english_text: str
    foreign_text: str
    clean_foreign_text: str = ""
    foreign_added_text: str = ""
    foreign_transliteration: str = ""
    foreign_ipa: str = ""
    foreign_gloss: str = ""
    front_gloss_defs: str = ""
    back_gloss_defs: str = ""
    thumbnail_name: str = ""
    media_name: str = ""
    tts_name: str = ""
    deck_name: str = ""
    is_reverse: bool = False
    is_map_card: bool = False
    map_front_image_name: str = ""
    map_back_image_name: str = ""


class DeckError(RuntimeError):
    pass


class TTSBackend:
    def synthesize(self, text: str, language_code: str, output_path: Path) -> bool:
        raise NotImplementedError


class MacOSSayTTS(TTSBackend):
    def __init__(self, preferred_voice: Optional[str] = None):
        self.preferred_voice = preferred_voice
        self._voices = self._load_voices()

    @staticmethod
    def _load_voices() -> Dict[str, str]:
        if shutil.which("say") is None:
            return {}
        proc = run(["say", "-v", "?"], capture_output=True)
        voices: Dict[str, str] = {}
        for line in proc.stdout.splitlines():
            m = re.match(r"^(\S+)\s+([a-z_\-]+)\s+", line.strip(), re.IGNORECASE)
            if m:
                voices[m.group(1)] = m.group(2)
        return voices

    def synthesize(self, text: str, language_code: str, output_path: Path) -> bool:
        if shutil.which("say") is None:
            return False
        voice = self.preferred_voice or MACOS_SAY_VOICES.get(language_code)
        cmd = ["say", "-o", str(output_path)]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        try:
            run(cmd)
            return output_path.exists() and output_path.stat().st_size > 0
        except subprocess.CalledProcessError:
            if voice:
                # Retry without specifying a voice, allowing the OS default.
                try:
                    run(["say", "-o", str(output_path), text])
                    return output_path.exists() and output_path.stat().st_size > 0
                except subprocess.CalledProcessError:
                    return False
            return False


class SherpaOnnxTTS(TTSBackend):
    def __init__(
        self,
        model_dir: Path,
        tokens_file: Optional[Path] = None,
        data_dir: Optional[Path] = None,
        dict_dir: Optional[Path] = None,
        rule_fsts: Optional[Path] = None,
        rule_fars: Optional[Path] = None,
    ):
        self.model_dir = model_dir
        self.tokens_file = tokens_file or (model_dir / "tokens.txt")

        default_data_dir = model_dir / "espeak-ng-data"
        self.data_dir = data_dir or (default_data_dir if default_data_dir.exists() else None)

        self.dict_dir = dict_dir
        self.rule_fsts = rule_fsts
        self.rule_fars = rule_fars

        self._tts = None
        self._tts_error = None

    def synthesize(self, text: str, language_code: str, output_path: Path) -> bool:
        if sherpa_onnx is None:
            raise DeckError(
                f"Sherpa Python package is not importable in this Python environment: {SHERPA_IMPORT_ERROR}"
            )

        if sf is None:
            raise DeckError(
                f"Python package 'soundfile' is not importable in this Python environment: {SOUNDFILE_IMPORT_ERROR}"
            )

        if not text.strip():
            return False

        tts = self._get_tts()

        gen_config = sherpa_onnx.GenerationConfig()
        gen_config.sid = 0
        gen_config.speed = 1.0
        gen_config.silence_scale = 0.2

        try:
            audio = tts.generate(text, gen_config)
        except Exception as exc:
            raise DeckError(f"Sherpa generation error: {exc}")

        if len(audio.samples) == 0:
            raise DeckError("Sherpa generated zero audio samples.")

        try:
            sf.write(
                str(output_path),
                audio.samples,
                samplerate=audio.sample_rate,
                subtype="PCM_16",
            )
        except Exception as exc:
            raise DeckError(f"Failed to save Sherpa WAV output: {exc}")

        return output_path.exists() and output_path.stat().st_size > 0

    def _get_tts(self):
        if self._tts is not None:
            return self._tts

        if self._tts_error is not None:
            raise DeckError(f"Sherpa TTS initialization already failed: {self._tts_error}")

        model_file = self._discover_model_file()
        if model_file is None:
            raise DeckError(f"No .onnx model file found in Sherpa model dir: {self.model_dir}")

        if not self.tokens_file.exists():
            raise DeckError(f"tokens.txt not found in Sherpa model dir: {self.tokens_file}")

        try:
            config = sherpa_onnx.OfflineTtsConfig(
                model=sherpa_onnx.OfflineTtsModelConfig(
                    vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                        model=str(model_file),
                        lexicon=str(self.dict_dir) if self.dict_dir and self.dict_dir.exists() else "",
                        data_dir=str(self.data_dir) if self.data_dir and self.data_dir.exists() else "",
                        tokens=str(self.tokens_file),
                    ),
                    provider="cpu",
                    debug=True,
                    num_threads=1,
                ),
                rule_fsts=str(self.rule_fsts) if self.rule_fsts and self.rule_fsts.exists() else "",
                max_num_sentences=1,
            )

            if not config.validate():
                raise DeckError(
                    "Sherpa config validation failed. Check that the model directory contains the correct .onnx file, tokens.txt, and any required espeak-ng-data folder."
                )

            self._tts = sherpa_onnx.OfflineTts(config)
            return self._tts

        except Exception as exc:
            self._tts_error = str(exc)
            raise DeckError(f"Sherpa initialization error: {exc}")

    def _discover_model_file(self) -> Optional[Path]:
        preferred_names = [
            "model.onnx",
            "generator.onnx",
        ]
        for name in preferred_names:
            candidate = self.model_dir / name
            if candidate.exists():
                return candidate

        candidates = sorted(self.model_dir.glob("*.onnx"))
        return candidates[0] if candidates else None


class AutoTTS(TTSBackend):
    def __init__(self, sherpa: Optional[SherpaOnnxTTS], say_backend: Optional[MacOSSayTTS]):
        self.sherpa = sherpa
        self.say_backend = say_backend

    def synthesize(self, text: str, language_code: str, output_path: Path) -> bool:
        if self.sherpa and self.sherpa.synthesize(text, language_code, output_path):
            return True
        if self.say_backend and self.say_backend.synthesize(text, language_code, output_path):
            return True
        return False


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-").lower()
    return value or "item"


def stable_int(seed: str, digits: int = 10) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest[:digits], 16)


def foreign_html_text(text: str, added_text: str = "") -> str:
    text = text.replace("\\N", "\n").replace("\\n", "\n").strip()
    added_text = added_text.replace("\\N", "\n").replace("\\n", "\n").strip()

    full_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not full_lines:
        return ""

    if not added_text:
        escaped_lines = [html.escape(line, quote=False) for line in full_lines]
        return "<br>".join(escaped_lines)

    added_lines = [line.strip() for line in added_text.splitlines() if line.strip()]
    if not added_lines:
        escaped_lines = [html.escape(line, quote=False) for line in full_lines]
        return "<br>".join(escaped_lines)

    if len(added_lines) <= len(full_lines) and full_lines[-len(added_lines):] == added_lines:
        normal_lines = full_lines[:-len(added_lines)]
        normal_html = "<br>".join(html.escape(line, quote=False) for line in normal_lines)
        added_html = "<br>".join(html.escape(line, quote=False) for line in added_lines)

        parts = []
        if normal_html:
            parts.append(normal_html)
        parts.append(f'<span class="subtitle-bottom-line">{added_html}</span>')
        return "<br>".join(parts)

    escaped_lines = [html.escape(line, quote=False) for line in full_lines]
    return "<br>".join(escaped_lines)


GLOSS_SPLIT_CHARS = " +≈≠≤≥?√$<>⟨⟩~()[]\\|/&;:→›>-=꞊‿._"
GLOSS_SPLIT_RE = re.compile(f"[{re.escape(GLOSS_SPLIT_CHARS)}]+")

ROMAN_NUMERALS_I_TO_XXV = [
    "XXV", "XXIV", "XXIII", "XXII", "XXI", "XX",
    "XIX", "XVIII", "XVII", "XVI", "XV", "XIV", "XIII", "XII", "XI", "X",
    "IX", "VIII", "VII", "VI", "V", "IV", "III", "II", "I",
]

GLOSS_PREFIX_RE = re.compile(
    r"^(12|13|[1234]|" + "|".join(ROMAN_NUMERALS_I_TO_XXV) + r")"
)

_ABBREVIATIONS_CACHE: Optional[Dict[str, str]] = None


def load_abbreviations() -> Dict[str, str]:
    global _ABBREVIATIONS_CACHE

    if _ABBREVIATIONS_CACHE is not None:
        return _ABBREVIATIONS_CACHE

    abbreviations_path = Path(__file__).with_name("abbreviations.json")

    if not abbreviations_path.is_file():
        raise DeckError(
            f"Gloss definitions require abbreviations.json next to this script: {abbreviations_path}"
        )

    try:
        with abbreviations_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        raise DeckError(f"Could not read abbreviations.json: {exc}")

    if not isinstance(payload, dict):
        raise DeckError("abbreviations.json must contain a top-level JSON object.")

    _ABBREVIATIONS_CACHE = {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(value, str)
    }
    return _ABBREVIATIONS_CACHE


def person_or_class_prefix_text(prefix: str) -> str:
    if prefix == "1":
        return "1st person "
    if prefix == "2":
        return "2nd person "
    if prefix == "3":
        return "3rd person "
    if prefix == "12":
        return "1st and 2nd person "
    if prefix == "13":
        return "1st and 3rd person "
    if prefix == "4":
        return "[4th person](https://en.wikipedia.org/wiki/Obviative) (= OBV), or 1st person inclusive or indefinite person, "
    if prefix in ROMAN_NUMERALS_I_TO_XXV:
        return f"[noun class](https://en.wikipedia.org/wiki/Noun_class) gender {prefix} "
    return ""


def markdown_links_to_html(text: str) -> str:
    """
    Convert basic markdown links to HTML links while escaping all other text.

    Supports:
      [text](https://example.com)
      [text](https://example.com "Title")
      [text](https://en.wikipedia.org/wiki/Copula_(linguistics))
    """
    if not text:
        return ""

    pieces: List[str] = []
    i = 0
    n = len(text)

    while i < n:
        label_start = text.find("[", i)
        if label_start == -1:
            pieces.append(html.escape(text[i:], quote=False))
            break

        label_end = text.find("]", label_start + 1)
        if label_end == -1 or label_end + 1 >= n or text[label_end + 1] != "(":
            pieces.append(html.escape(text[i:label_start + 1], quote=False))
            i = label_start + 1
            continue

        pieces.append(html.escape(text[i:label_start], quote=False))

        url_start = label_end + 2
        pos = url_start
        paren_depth = 0
        url_end = -1

        while pos < n:
            ch = text[pos]

            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                if paren_depth == 0:
                    url_end = pos
                    break
                paren_depth -= 1

            pos += 1

        if url_end == -1:
            pieces.append(html.escape(text[label_start:], quote=False))
            break

        label = text[label_start + 1:label_end]
        destination = text[url_start:url_end].strip()

        title = ""
        url = destination

        title_match = re.match(r'^(.*?)\s+"([^"]*)"\s*$', destination)
        if title_match:
            url = title_match.group(1).strip()
            title = title_match.group(2)

        label_html = html.escape(label, quote=False)
        url_html = html.escape(url, quote=True)

        if title:
            title_html = html.escape(title, quote=True)
            pieces.append(f'<a href="{url_html}" title="{title_html}">{label_html}</a>')
        else:
            pieces.append(f'<a href="{url_html}">{label_html}</a>')

        i = url_end + 1

    return "".join(pieces)


def lookup_gloss_definition(raw_key: str, abbreviations: Dict[str, str]) -> Optional[str]:
    if raw_key in abbreviations:
        return abbreviations[raw_key]

    prefix_match = GLOSS_PREFIX_RE.match(raw_key)
    if not prefix_match:
        return None

    prefix = prefix_match.group(1)
    stripped_key = raw_key[len(prefix):]

    if not stripped_key:
        return None

    if stripped_key not in abbreviations:
        return None

    return person_or_class_prefix_text(prefix) + abbreviations[stripped_key]


def build_gloss_definitions_html(gloss_text: str) -> str:
    gloss_text = (gloss_text or "").strip()
    if not gloss_text:
        return ""

    abbreviations = load_abbreviations()
    if not abbreviations:
        return ""

    entries: List[str] = []
    seen = set()

    for raw_piece in GLOSS_SPLIT_RE.split(gloss_text):
        key = raw_piece.strip()
        if not key:
            continue

        definition = lookup_gloss_definition(key, abbreviations)
        if not definition:
            continue

        entry_html = (
            f'<span class="gloss-key">{html.escape(key, quote=False)}</span> = '
            f"{markdown_links_to_html(definition)}"
        )

        if entry_html in seen:
            continue

        seen.add(entry_html)
        entries.append(entry_html)

    if not entries:
        return ""

    return "<br>".join(entries)


def append_gloss_defs_html(main_html: str, gloss_defs_html: str) -> str:
    gloss_defs_html = (gloss_defs_html or "").strip()
    if not gloss_defs_html:
        return main_html

    if main_html:
        return f'{main_html}<div class="gloss-definitions">{gloss_defs_html}</div>'

    return f'<div class="gloss-definitions">{gloss_defs_html}</div>'


def html_text(text: str) -> str:
    text = text.replace("\\N", "\n").replace("\\n", "\n")
    text = text.strip()
    text = html.escape(text, quote=False)
    text = re.sub(r"\s*\n\s*", "<br>", text)
    return text


def small_text_html(text: str) -> str:
    text = text.replace("\\N", "\n").replace("\\n", "\n").strip()
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    escaped = "<br>".join(html.escape(line, quote=False) for line in lines)
    return f'<span class="subtitle-bottom-line">{escaped}</span>'


def combine_main_and_small_text(main_text: str, *small_parts: str) -> str:
    pieces: List[str] = []

    main_html = html_text(main_text)
    if main_html:
        pieces.append(main_html)

    for part in small_parts:
        part_html = small_text_html(part)
        if part_html:
            pieces.append(part_html)

    return "<br>".join(pieces)


def find_map_json_path(root: Path) -> Optional[Path]:
    for p in root.iterdir():
        if p.is_file() and p.name.lower() == "map.json":
            return p
    return None


def load_map_json(map_path: Path) -> dict:
    try:
        with map_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
        raise DeckError(f"Could not read Map.json: {exc}")

    if not isinstance(payload, dict):
        raise DeckError("Map.json must contain a top-level JSON object.")

    metadata = payload.get("metadata")
    segments = payload.get("segments")

    if not isinstance(metadata, dict):
        raise DeckError("Map.json is missing a valid 'metadata' object.")
    if not isinstance(segments, list):
        raise DeckError("Map.json is missing a valid 'segments' list.")

    return payload


def determine_map_languages(payload: dict) -> Tuple[str, str]:
    metadata = payload.get("metadata", {})
    source_lang = infer_language_code(str(metadata.get("source_language", "")).strip())
    target_lang = infer_language_code(str(metadata.get("target_language", "")).strip())

    if not source_lang or not target_lang:
        raise DeckError("Map.json metadata must include readable source_language and target_language values.")

    if source_lang == "en" and target_lang != "en":
        return "source", "target"

    if target_lang == "en" and source_lang != "en":
        return "target", "source"

    raise DeckError(
        "Map.json currently requires exactly one side to be English ('en') and the other side to be the foreign language."
    )


def build_map_aligned_pairs(
    payload: dict,
) -> Tuple[List[Tuple[SubtitleLine, List[SubtitleLine]]], Dict[int, dict], str]:
    english_side, foreign_side = determine_map_languages(payload)
    segments = payload.get("segments", [])

    aligned_pairs: List[Tuple[SubtitleLine, List[SubtitleLine]]] = []
    extras_by_index: Dict[int, dict] = {}

    prepared_segments: List[dict] = []

    for segment in segments:
        if not isinstance(segment, dict):
            continue

        source = segment.get("source") or {}
        target = segment.get("target") or {}

        if not isinstance(source, dict) or not isinstance(target, dict):
            continue

        source_text = clean_subtitle_text(str(source.get("text", "") or ""))
        target_text = clean_subtitle_text(str(target.get("text", "") or ""))

        source_gloss = clean_subtitle_text(str(source.get("gloss", "") or ""))
        target_gloss = clean_subtitle_text(str(target.get("gloss", "") or ""))

        if english_side == "source":
            english_text = source_text
            foreign_text = target_text
            english_gloss = source_gloss
            foreign_gloss = target_gloss
            foreign_obj = target
        else:
            english_text = target_text
            foreign_text = source_text
            english_gloss = target_gloss
            foreign_gloss = source_gloss
            foreign_obj = source

        if not english_text or not foreign_text:
            continue

        prepared_segments.append(
            {
                "english_text": english_text,
                "foreign_text": foreign_text,
                "transliteration": clean_subtitle_text(str(foreign_obj.get("transliteration", "") or "")),
                "ipa": clean_subtitle_text(str(foreign_obj.get("ipa", "") or "")),
                "gloss": foreign_gloss,
                "english_gloss": english_gloss,
            }
        )

    if not prepared_segments:
        raise DeckError("Map.json did not contain any usable source/target text pairs.")

    current_ms = 0
    for idx, item in enumerate(prepared_segments):
        english_text = item["english_text"]
        foreign_text = item["foreign_text"]

        english_line = SubtitleLine(
            start_ms=current_ms,
            end_ms=current_ms + 1000,
            text_raw=english_text,
            text_plain=english_text,
        )
        foreign_line = SubtitleLine(
            start_ms=current_ms,
            end_ms=current_ms + 1000,
            text_raw=foreign_text,
            text_plain=foreign_text,
        )

        aligned_pairs.append((foreign_line, [english_line]))

        prev_items = prepared_segments[max(0, idx - 2):idx]
        next_items = prepared_segments[idx + 1:idx + 3]

        extras_by_index[idx] = {
            "transliteration": item["transliteration"],
            "ipa": item["ipa"],
            "gloss": item["gloss"],
            "english_gloss": item["english_gloss"],
            "prev_english_texts": [x["english_text"] for x in prev_items],
            "next_english_texts": [x["english_text"] for x in next_items],
            "prev_foreign_texts": [x["foreign_text"] for x in prev_items],
            "next_foreign_texts": [x["foreign_text"] for x in next_items],
        }

        current_ms += 1000

    metadata = payload.get("metadata", {})
    foreign_language_code = infer_language_code(
        str(metadata.get("target_language" if english_side == "source" else "source_language", "")).strip()
    )

    return aligned_pairs, extras_by_index, (foreign_language_code or "")


ASS_TAG_RE = re.compile(r"\{[^{}]*\}")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_subtitle_text(text: str) -> str:
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = ASS_TAG_RE.sub("", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    text = HTML_TAG_RE.sub("", text)
    text = text.replace("&nbsp;", " ")

    # Clean each line without destroying line breaks.
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)

    return "\n".join(lines).strip()


DIALOGUE_BLACKLIST = {
    "[music]", "(music)", "[applause]", "(applause)", "[laughter]", "(laughter)",
    "[inaudible]", "(inaudible)", "[silence]", "(silence)", "...",
}


def looks_like_dialogue(text: str) -> bool:
    if not text:
        return False

    normalized = " ".join(text.splitlines()).strip().lower()
    if normalized in DIALOGUE_BLACKLIST:
        return False

    letters = sum(ch.isalpha() for ch in normalized)
    return letters > 0


def load_subtitles(path: Path, fps: Optional[float] = None) -> List[SubtitleLine]:
    kwargs = {}
    if path.suffix.lower() == ".sub" and fps is not None:
        kwargs["fps"] = fps
    subs = pysubs2.load(str(path), **kwargs)
    lines: List[SubtitleLine] = []
    for item in subs:
        text_raw = getattr(item, "text", "") or ""
        text_plain = clean_subtitle_text(text_raw)
        start_ms = int(getattr(item, "start", 0))
        end_ms = int(getattr(item, "end", 0))
        if end_ms <= start_ms:
            continue
        if not looks_like_dialogue(text_plain):
            continue
        lines.append(SubtitleLine(start_ms, end_ms, text_raw, text_plain))
    return lines


def ms_to_ffmpeg_time(ms: int) -> str:
    sec = max(0, ms / 1000.0)
    return f"{sec:.3f}"


def probe_video_fps(video_path: Path) -> Optional[float]:
    if shutil.which("ffprobe") is None:
        return None
    proc = run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "json",
            str(video_path),
        ],
        capture_output=True,
    )
    try:
        payload = json.loads(proc.stdout)
        rate = payload["streams"][0]["r_frame_rate"]
        num, den = rate.split("/")
        num_f = float(num)
        den_f = float(den)
        if den_f == 0:
            return None
        return num_f / den_f
    except Exception:
        return None


def probe_stream_types(media_path: Path) -> Tuple[bool, bool]:
    if shutil.which("ffprobe") is None:
        return False, False

    proc = run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            str(media_path),
        ],
        capture_output=True,
    )

    try:
        payload = json.loads(proc.stdout)
        streams = payload.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return has_video, has_audio
    except Exception:
        return False, False


def media_has_video(media_path: Path) -> bool:
    has_video, _ = probe_stream_types(media_path)
    return has_video


def media_has_audio(media_path: Path) -> bool:
    _, has_audio = probe_stream_types(media_path)
    return has_audio


def find_input_files(root: Path) -> Tuple[Path, Path, Path, str]:
    media_files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    if len(media_files) != 1:
        raise DeckError(f"Expected exactly 1 media file in {root}, found {len(media_files)}")

    media = media_files[0]
    has_video, has_audio = probe_stream_types(media)

    if not has_video and not has_audio:
        raise DeckError(
            f"Input file is not a usable audio/video media file: {media.name}"
        )

    subs = [
        p for p in root.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUB_EXTS
        and p.name.lower() != "map.json"
    ]
    english = [p for p in subs if p.stem.strip().lower() == "english"]
    if len(english) != 1:
        raise DeckError("Expected exactly 1 English subtitle file named 'English.<ext>'")
    english_sub = english[0]

    # Ignore helper clean subtitle files when identifying the main foreign subtitle.
    foreign_candidates = [
        p for p in subs
        if p != english_sub and not p.stem.strip().lower().endswith("-clean")
    ]

    if len(foreign_candidates) != 1:
        raise DeckError(
            "Expected exactly 1 main foreign subtitle file besides English "
            "(excluding helper files like '-clean'). "
            "Name it with its language, e.g. 'Spanish.srt' or 'Japanese.ass'."
        )

    foreign_sub = foreign_candidates[0]
    language_name = foreign_sub.stem.strip()
    return media, english_sub, foreign_sub, language_name


def find_clean_tts_subtitle_path(foreign_sub_path: Path) -> Optional[Path]:
    """
    If the foreign subtitle is something like:
      Italian.srt
    and a matching clean file exists:
      Italian-clean.srt
    return the clean file path.

    Otherwise return None.

    We only look for the clean .srt version, because the requested behavior
    is specifically "[FOREIGN]-clean.srt version of [FOREIGN].srt".
    """
    clean_candidate = foreign_sub_path.with_name(f"{foreign_sub_path.stem}-clean.srt")
    if clean_candidate.is_file():
        return clean_candidate
    return None


def overlap_ms(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def exact_time_match(a: SubtitleLine, b: SubtitleLine) -> bool:
    return a.start_ms == b.start_ms and a.end_ms == b.end_ms


def near_time_match(a: SubtitleLine, b: SubtitleLine, tolerance_ms: int = 120) -> bool:
    return (
        abs(a.start_ms - b.start_ms) <= tolerance_ms
        and abs(a.end_ms - b.end_ms) <= tolerance_ms
    )


def time_distance(a: SubtitleLine, b: SubtitleLine) -> int:
    return abs(a.start_ms - b.start_ms) + abs(a.end_ms - b.end_ms)


def align_subtitles(
    foreign_lines: List[SubtitleLine],
    english_lines: List[SubtitleLine],
) -> List[Tuple[SubtitleLine, List[SubtitleLine]]]:
    aligned: List[Tuple[SubtitleLine, List[SubtitleLine]]] = []
    if not foreign_lines:
        return aligned

    e_idx = 0
    total_e = len(english_lines)

    for f in foreign_lines:
        # Move the English pointer forward past clearly earlier lines.
        while e_idx < total_e and english_lines[e_idx].end_ms < f.start_ms - 2000:
            e_idx += 1

        nearby = english_lines[max(0, e_idx - 3): min(total_e, e_idx + 6)]

        # 1) Best case: exact same time interval -> strict 1-to-1 match.
        exact_matches = [e for e in nearby if exact_time_match(f, e)]
        if exact_matches:
            aligned.append((f, [exact_matches[0]]))
            continue

        # 2) Small discrepancy fallback: allow only very close timing.
        near_matches = [e for e in nearby if near_time_match(f, e, tolerance_ms=120)]
        if near_matches:
            best = min(near_matches, key=lambda e: time_distance(f, e))
            aligned.append((f, [best]))
            continue

        # 3) Last resort: choose the single nearest English line.
        search_pool = english_lines[max(0, e_idx - 5): min(total_e, e_idx + 8)] or english_lines
        nearest = min(search_pool, key=lambda e: time_distance(f, e))
        aligned.append((f, [nearest]))

    return aligned


def choose_tts_text_for_line(
    foreign_line: SubtitleLine,
    clean_lines: Optional[List[SubtitleLine]],
) -> str:
    """
    Use the clean subtitle text for TTS when available.

    Matching priority:
    1. Exact same start/end
    2. Very close start/end
    3. Best overlap
    4. Nearest timing
    5. Fallback to the original foreign subtitle text
    """
    if not clean_lines:
        return foreign_line.text_plain

    # 1) Exact match
    for line in clean_lines:
        if exact_time_match(foreign_line, line):
            return line.text_plain or foreign_line.text_plain

    # 2) Near match
    near_matches = [line for line in clean_lines if near_time_match(foreign_line, line, tolerance_ms=120)]
    if near_matches:
        best = min(near_matches, key=lambda line: time_distance(foreign_line, line))
        return best.text_plain or foreign_line.text_plain

    # 3) Best overlap
    overlapping = [
        (overlap_ms(foreign_line.start_ms, foreign_line.end_ms, line.start_ms, line.end_ms), line)
        for line in clean_lines
    ]
    overlapping = [(ov, line) for ov, line in overlapping if ov > 0]
    if overlapping:
        best_overlap, best_line = max(overlapping, key=lambda item: (item[0], -time_distance(foreign_line, item[1])))
        if best_overlap > 0:
            return best_line.text_plain or foreign_line.text_plain

    # 4) Nearest timing
    best = min(clean_lines, key=lambda line: time_distance(foreign_line, line))
    return best.text_plain or foreign_line.text_plain


def choose_clean_text_for_line(
    foreign_line: SubtitleLine,
    clean_lines: Optional[List[SubtitleLine]],
) -> str:
    """
    Return the matching clean subtitle text when available.
    If no clean subtitle file exists, return an empty string.
    """
    if not clean_lines:
        return ""

    for line in clean_lines:
        if exact_time_match(foreign_line, line):
            return line.text_plain

    near_matches = [line for line in clean_lines if near_time_match(foreign_line, line, tolerance_ms=120)]
    if near_matches:
        best = min(near_matches, key=lambda line: time_distance(foreign_line, line))
        return best.text_plain

    overlapping = [
        (overlap_ms(foreign_line.start_ms, foreign_line.end_ms, line.start_ms, line.end_ms), line)
        for line in clean_lines
    ]
    overlapping = [(ov, line) for ov, line in overlapping if ov > 0]
    if overlapping:
        _, best_line = max(overlapping, key=lambda item: (item[0], -time_distance(foreign_line, item[1])))
        return best_line.text_plain

    best = min(clean_lines, key=lambda line: time_distance(foreign_line, line))
    return best.text_plain


def extract_added_text_from_clean(full_text: str, clean_text: str) -> str:
    """
    Return only the text that was added in the full subtitle compared to the clean subtitle.

    Expected common case:
      clean subtitle = first part
      full subtitle  = clean subtitle + added trailing line(s)

    If there is no clear additive suffix, return an empty string.
    """
    full_text = full_text.replace("\\N", "\n").replace("\\n", "\n").strip()
    clean_text = clean_text.replace("\\N", "\n").replace("\\n", "\n").strip()

    if not full_text or not clean_text or full_text == clean_text:
        return ""

    full_lines = [line.strip() for line in full_text.splitlines() if line.strip()]
    clean_lines = [line.strip() for line in clean_text.splitlines() if line.strip()]

    if not full_lines or not clean_lines:
        return ""

    if len(full_lines) > len(clean_lines) and full_lines[:len(clean_lines)] == clean_lines:
        return "\n".join(full_lines[len(clean_lines):]).strip()

    if full_text.startswith(clean_text):
        remainder = full_text[len(clean_text):].strip()
        return remainder

    return ""


def split_foreign_text_using_clean(full_text: str, clean_text: str) -> Tuple[str, str]:
    """
    Return:
      kept_text  = what should stay as the main foreign subtitle text
      added_text = only the extra text compared to the clean subtitle
    """
    added_text = extract_added_text_from_clean(full_text, clean_text)

    if not added_text:
        return full_text.strip(), ""

    kept_text = clean_text.strip()
    if not kept_text:
        kept_text = full_text.strip()

    return kept_text, added_text


def midpoint_ms(start_ms: int, end_ms: int) -> int:
    if end_ms <= start_ms:
        return start_ms
    return start_ms + (end_ms - start_ms) // 2


def ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise DeckError(f"Required tool not found on PATH: {name}")


def probe_video_duration_ms(video_path: Path) -> Optional[int]:
    if shutil.which("ffprobe") is None:
        return None
    proc = run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
    )
    raw = (proc.stdout or "").strip()
    try:
        return max(0, int(float(raw) * 1000))
    except (TypeError, ValueError):
        return None


def clamp_thumbnail_time_ms(at_ms: int, video_duration_ms: Optional[int]) -> int:
    if video_duration_ms is None:
        return max(0, at_ms)
    return max(0, min(at_ms, max(0, video_duration_ms - 100)))


def ffmpeg_thumbnail(media_path: Path, output_path: Path, at_ms: int, width: int) -> bool:
    if not media_has_video(media_path):
        return False

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        ms_to_ffmpeg_time(at_ms),
        "-i",
        str(media_path),
        "-frames:v",
        "1",
        "-update",
        "1",
        "-vf",
        f"scale='min({width},iw)':-2,format=yuvj420p",
        "-q:v",
        "2",
        str(output_path),
    ]
    run(cmd)
    return output_path.exists() and output_path.stat().st_size > 0


def ffmpeg_clip(
    media_path: Path,
    output_path: Path,
    start_ms: int,
    end_ms: int,
    height: int,
    crf: int,
) -> None:
    duration_ms = max(250, end_ms - start_ms)

    if media_has_video(media_path):
        cmd = [
            "ffmpeg", "-y",
            "-ss", ms_to_ffmpeg_time(start_ms),
            "-t", ms_to_ffmpeg_time(duration_ms),
            "-i", str(media_path),
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", str(crf),
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-ss", ms_to_ffmpeg_time(start_ms),
            "-t", ms_to_ffmpeg_time(duration_ms),
            "-i", str(media_path),
            "-vn",
            "-c:a", "libmp3lame",
            "-q:a", "2",
            str(output_path),
        ]

    run(cmd)


def transcode_audio_to_mp3(input_audio: Path, output_audio: Path) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_audio),
        "-vn",
        "-codec:a", "libmp3lame",
        "-q:a", "2",
        str(output_audio),
    ]
    run(cmd)


def synthesize_tts_mp3(
    *,
    tts_backend: Optional[TTSBackend],
    media_dir: Path,
    safe_prefix: str,
    text: str,
    language_code: Optional[str],
    warnings: List[str],
    card_idx: int,
) -> str:
    if not text:
        warnings.append(f"TTS skipped for card {card_idx}: empty foreign text.")
        return ""

    if not language_code:
        warnings.append(f"TTS skipped for card {card_idx}: foreign language code is unknown.")
        return ""

    if language_code not in SUPPORTED_LANGUAGES:
        warnings.append(
            f"TTS skipped for card {card_idx}: language code '{language_code}' is not in the built-in voice map."
        )
        return ""

    if tts_backend is None:
        warnings.append(f"TTS skipped for card {card_idx}: no TTS backend configured.")
        return ""

    raw_tts = media_dir / f"{safe_prefix}.wav"
    mp3_tts = media_dir / f"{safe_prefix}.mp3"

    success = tts_backend.synthesize(text, language_code, raw_tts)
    if not success:
        warnings.append(
            f"TTS skipped for card {card_idx}: local engine could not synthesize language '{language_code}'."
        )
        return ""

    try:
        transcode_audio_to_mp3(raw_tts, mp3_tts)
        raw_tts.unlink(missing_ok=True)
        return mp3_tts.name
    except subprocess.CalledProcessError:
        warnings.append(f"TTS generated but failed to transcode for card {card_idx}.")
        return ""


def load_page_font(font_size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
        "/System/Library/Fonts/Supplemental/Georgia.ttf",
        "/System/Library/Fonts/Supplemental/Palatino.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def wrap_text_for_width(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont, max_width: int) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: List[str] = []

    for paragraph in paragraphs:
        paragraph = " ".join(paragraph.split())
        if not paragraph:
            lines.append("")
            continue

        words = paragraph.split()
        current = words[0]

        for word in words[1:]:
            candidate = f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font)
            width = bbox[2] - bbox[0]
            if width <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word

        lines.append(current)

    return lines


def draw_lines_centered(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    *,
    font: ImageFont.ImageFont,
    fill: Tuple[int, int, int, int],
    image_width: int,
    y_start: int,
    line_gap: int,
) -> int:
    y = y_start
    for line in lines:
        bbox = draw.textbbox((0, 0), line if line else " ", font=font)
        line_width = bbox[2] - bbox[0]
        line_height = bbox[3] - bbox[1]
        x = (image_width - line_width) // 2
        draw.text((x, y), line if line else " ", font=font, fill=fill)
        y += line_height + line_gap
    return y


def estimate_block_height(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    *,
    font: ImageFont.ImageFont,
    line_gap: int,
) -> int:
    if not lines:
        return 0

    total = 0
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line if line else " ", font=font)
        line_height = bbox[3] - bbox[1]
        total += line_height
        if i < len(lines) - 1:
            total += line_gap
    return total


def create_map_context_image(
    *,
    prev_texts: List[str],
    current_text: str,
    next_texts: List[str],
    output_path: Path,
    image_width: int = 900,
    horizontal_padding: int = 85,
    top_bottom_padding: int = 55,
    block_gap: int = 28,
    line_gap: int = 10,
    background_hex: str = "#f1e1c9",
) -> bool:
    current_text = (current_text or "").strip()
    if not current_text:
        return False

    font = load_page_font(34)
    dummy = Image.new("RGBA", (image_width, 100), (255, 255, 255, 0))
    dummy_draw = ImageDraw.Draw(dummy)
    usable_width = image_width - (horizontal_padding * 2)

    prev_blocks = []
    for text in prev_texts:
        lines = wrap_text_for_width(text, dummy_draw, font, usable_width)
        if lines:
            prev_blocks.append(lines)

    current_lines = wrap_text_for_width(current_text, dummy_draw, font, usable_width)

    next_blocks = []
    for text in next_texts:
        lines = wrap_text_for_width(text, dummy_draw, font, usable_width)
        if lines:
            next_blocks.append(lines)

    current_height = estimate_block_height(dummy_draw, current_lines, font=font, line_gap=line_gap)

    def blocks_height(blocks: List[List[str]]) -> int:
        if not blocks:
            return 0
        total = 0
        for i, block in enumerate(blocks):
            total += estimate_block_height(dummy_draw, block, font=font, line_gap=line_gap)
            if i < len(blocks) - 1:
                total += block_gap
        return total

    prev_height = blocks_height(prev_blocks)
    next_height = blocks_height(next_blocks)

    blank_context_height = current_height

    top_context_height = prev_height if prev_blocks else blank_context_height
    bottom_context_height = next_height if next_blocks else blank_context_height

    image_height = (
        top_bottom_padding
        + top_context_height
        + block_gap
        + current_height
        + block_gap
        + bottom_context_height
        + top_bottom_padding
    )

    image = Image.new("RGBA", (image_width, image_height), background_hex)
    draw = ImageDraw.Draw(image)

    context_layer = Image.new("RGBA", (image_width, image_height), (0, 0, 0, 0))
    context_draw = ImageDraw.Draw(context_layer)

    current_fill = (0, 0, 0, 255)
    context_fill = (0, 0, 0, 128)

    y = top_bottom_padding

    if prev_blocks:
        for block_idx, block_lines in enumerate(prev_blocks):
            y = draw_lines_centered(
                context_draw,
                block_lines,
                font=font,
                fill=context_fill,
                image_width=image_width,
                y_start=y,
                line_gap=line_gap,
            )
            if block_idx < len(prev_blocks) - 1:
                y += block_gap
    else:
        y += blank_context_height

    y += block_gap

    y = draw_lines_centered(
        draw,
        current_lines,
        font=font,
        fill=current_fill,
        image_width=image_width,
        y_start=y,
        line_gap=line_gap,
    )

    y += block_gap

    if next_blocks:
        for block_idx, block_lines in enumerate(next_blocks):
            y = draw_lines_centered(
                context_draw,
                block_lines,
                font=font,
                fill=context_fill,
                image_width=image_width,
                y_start=y,
                line_gap=line_gap,
            )
            if block_idx < len(next_blocks) - 1:
                y += block_gap

    image = Image.alpha_composite(image, context_layer)
    image.save(output_path, format="PNG")
    return output_path.exists() and output_path.stat().st_size > 0


def make_part_label(part_number: int) -> str:
    if part_number < 10:
        return f"Part 0{part_number}"
    return f"Part {part_number}"


def make_directional_deck_name(
    parent_deck_name: str,
    *,
    part_number: int,
    split_enabled: bool,
    foreign_language_code: Optional[str],
    reverse: bool,
) -> str:
    foreign_code = (foreign_language_code or "FL").upper()

    if reverse:
        direction = f"{foreign_code} \u2192 EN"
    else:
        direction = f"EN \u2192 {foreign_code}"

    if split_enabled:
        return f"{parent_deck_name}::{make_part_label(part_number)} {direction}"

    return f"{parent_deck_name}::{direction}"


def compute_part_number(
    card_index_zero_based: int,
    clip_start_ms: int,
    split_every_n_cards: int,
    split_every_minutes: float,
) -> int:
    if split_every_n_cards:
        return (card_index_zero_based // split_every_n_cards) + 1

    if split_every_minutes:
        window_ms = int(split_every_minutes * 60 * 1000)
        return (clip_start_ms // window_ms) + 1

    return 1


def stable_deck_id(deck_name: str, tag: str) -> int:
    return stable_int(f"deck::{deck_name}::{tag}")


def build_tts_backend(args: argparse.Namespace) -> Optional[TTSBackend]:
    if args.tts_engine == "none":
        return None

    say_backend = MacOSSayTTS(preferred_voice=args.say_voice) if sys.platform == "darwin" else None
    sherpa_backend = None

    if args.tts_engine in {"auto", "sherpa"} and args.sherpa_model_dir:
        sherpa_backend = SherpaOnnxTTS(
            model_dir=Path(args.sherpa_model_dir),
            tokens_file=Path(args.sherpa_tokens) if args.sherpa_tokens else None,
            data_dir=Path(args.sherpa_data_dir) if args.sherpa_data_dir else None,
            dict_dir=Path(args.sherpa_dict_dir) if args.sherpa_dict_dir else None,
            rule_fsts=Path(args.sherpa_rule_fsts) if args.sherpa_rule_fsts else None,
            rule_fars=Path(args.sherpa_rule_fars) if args.sherpa_rule_fars else None,
        )

    if args.tts_engine == "say":
        return say_backend
    if args.tts_engine == "sherpa":
        return sherpa_backend
    return AutoTTS(sherpa=sherpa_backend, say_backend=say_backend)


def create_note_model(model_id: int) -> genanki.Model:
    return genanki.Model(
        model_id,
        "Video Dialogue Card",
        fields=[
            {"name": "FrontText"},
            {"name": "BackText"},
            {"name": "RemovedLastLine"},
            {"name": "FrontTextClass"},
            {"name": "BackTextClass"},
            {"name": "FrontVisual"},
            {"name": "BackVisual"},
            {"name": "FrontAudio"},
            {"name": "BackAudio"},
            {"name": "SortKey"},
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": """
{{#FrontVisual}}<div class=\"frame\">{{FrontVisual}}</div>{{/FrontVisual}}
<div class="line front-text {{FrontTextClass}}">{{FrontText}}</div>
{{#FrontAudio}}<div class=\"media\">{{FrontAudio}}</div>{{/FrontAudio}}
""",
                "afmt": """
{{FrontSide}}
<hr id=answer>
{{#RemovedLastLine}}<div class="line"><span class="subtitle-bottom-line">{{RemovedLastLine}}</span></div>{{/RemovedLastLine}}
{{#BackVisual}}<div class=\"frame\">{{BackVisual}}</div>{{/BackVisual}}
<div class="line back-text {{BackTextClass}}">{{BackText}}</div>
{{#BackAudio}}<div class=\"media\">{{BackAudio}}</div>{{/BackAudio}}
""",
            }
        ],
        css="""
.card {
  font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, sans-serif;
  font-size: 22px;
  text-align: center;
  color: #111;
  background: white;
}
.frame img {
  max-width: 95%;
  height: auto;
  border-radius: 8px;
}
.line {
  margin-top: 16px;
  line-height: 1.45;
}
.front-text.english-like {
  font-size: 1.05em;
}

.front-text.foreign-like {
  font-size: 1.2em;
  font-weight: 600;
}

.back-text.english-like {
  font-size: 1.05em;
}

.back-text.foreign-like {
  font-size: 1.2em;
  font-weight: 600;
}
.media {
  margin-top: 14px;
}
.subtitle-bottom-line {
  font-size: 0.72em;
  font-style: italic;
}
.gloss-definitions {
  margin-top: 18px;
  font-size: 0.72em;
  font-style: italic;
  font-weight: normal;
  line-height: 1.35;
  color: #ffaa00;
}
.gloss-definitions a {
  color: #00ffff;
  text-decoration: underline;
}
.gloss-key {
  font-weight: bold;
  text-decoration: underline;
}
#answer {
  margin-top: 18px;
  margin-bottom: 18px;
}
""",
        sort_field_index=5,
    )


def note_for_direction(
    *,
    model,
    card: CardItem,
    tag: str,
    source_language: str,
) -> genanki.Note:
    clip_tag = f'[sound:{card.media_name}]' if card.media_name else ""
    tts_tag = f'[sound:{card.tts_name}]' if card.tts_name else ""
    thumb_tag = f'<img src="{html.escape(card.thumbnail_name)}">' if card.thumbnail_name else ""
    map_front_tag = f'<img src="{html.escape(card.map_front_image_name)}">' if card.map_front_image_name else ""
    map_back_tag = f'<img src="{html.escape(card.map_back_image_name)}">' if card.map_back_image_name else ""

    def with_front_gloss(text_html: str) -> str:
        return append_gloss_defs_html(text_html, card.front_gloss_defs)

    def with_back_gloss(text_html: str) -> str:
        return append_gloss_defs_html(text_html, card.back_gloss_defs)

    if card.is_map_card:
        map_front_foreign = combine_main_and_small_text(
            card.foreign_text,
            card.foreign_transliteration,
            card.foreign_ipa,
        )

        map_back_foreign = combine_main_and_small_text(
            card.foreign_text,
            card.foreign_transliteration,
            card.foreign_ipa,
            card.foreign_gloss,
        )

        removed_last_line = card.foreign_gloss.strip()

        if card.is_reverse:
            front_text = map_front_foreign
            back_text = html_text(card.english_text)
            if map_front_tag:
                front_text = f'{front_text}<div class="frame">{map_front_tag}</div>'
            if map_back_tag:
                back_text = f'{back_text}<div class="frame">{map_back_tag}</div>'
            front_visual = ""
            back_visual = ""
            front_audio = tts_tag
            back_audio = ""
        else:
            front_text = html_text(card.english_text)
            back_text = map_back_foreign
            if map_front_tag:
                front_text = f'{front_text}<div class="frame">{map_front_tag}</div>'
            if map_back_tag:
                back_text = f'{back_text}<div class="frame">{map_back_tag}</div>'
            front_visual = ""
            back_visual = ""
            front_audio = ""
            back_audio = tts_tag

        guid = genanki.guid_for(
            tag, "reverse" if card.is_reverse else "normal", card.idx, card.start_ms, card.end_ms, card.foreign_text
        )
        return genanki.Note(
            model=model,
            fields=[
                with_front_gloss(front_text),
                with_back_gloss(back_text),
                html.escape(removed_last_line if card.is_reverse else "", quote=False),
                "foreign-like" if card.is_reverse else "english-like",
                "english-like" if card.is_reverse else "foreign-like",
                front_visual,
                back_visual,
                front_audio,
                back_audio,
                f"{card.idx:08d}",
            ],
            tags=[tag, "reverse" if card.is_reverse else "normal"],
            guid=guid,
        )

    # From here down, keep the original media/subtitle behavior intact.
    if card.is_reverse:
        if card.clean_foreign_text and card.foreign_added_text:
            front_foreign, removed_last_line = split_foreign_text_using_clean(
                card.foreign_text,
                card.clean_foreign_text,
            )
            if not front_foreign:
                front_foreign = card.foreign_text.strip()
        else:
            front_foreign = card.foreign_text.strip()
            removed_last_line = ""

        if source_language == "english":
            # Reverse English source:
            # Front = foreign subtitles + TTS
            # Back = video clip + English subtitles
            front_text = foreign_html_text(front_foreign)
            back_text = html_text(card.english_text)
            front_visual = ""
            front_audio = tts_tag
            back_audio = clip_tag
        else:
            # Reverse non-English source:
            # Front = foreign subtitles + video clip
            # Back = English subtitles
            front_text = foreign_html_text(front_foreign)
            back_text = html_text(card.english_text)
            front_visual = ""
            front_audio = clip_tag
            back_audio = ""

        guid = genanki.guid_for(
            tag, "reverse", card.idx, card.start_ms, card.end_ms, card.foreign_text
        )
        return genanki.Note(
            model=model,
            fields=[
                with_front_gloss(front_text),
                with_back_gloss(back_text),
                html.escape(removed_last_line, quote=False),
                "foreign-like",
                "english-like",
                front_visual,
                "",
                front_audio,
                back_audio,
                f"{card.idx:08d}",
            ],
            tags=[tag, "reverse"],
            guid=guid,
        )

    if source_language == "english":
        # Normal English source:
        # Front = video clip + English subtitles
        # Back = Foreign subtitles + TTS
        front_text = html_text(card.english_text)
        back_text = foreign_html_text(card.foreign_text, card.foreign_added_text)
        front_visual = ""
        front_audio = clip_tag
        back_audio = tts_tag
    else:
        # Normal non-English source:
        # Front = Thumbnail + English subtitles
        # Back = Foreign subtitles + clip
        front_text = html_text(card.english_text)
        back_text = foreign_html_text(card.foreign_text, card.foreign_added_text)
        front_visual = thumb_tag
        front_audio = ""
        back_audio = clip_tag

    guid = genanki.guid_for(
        tag, "normal", card.idx, card.start_ms, card.end_ms, card.foreign_text
    )
    return genanki.Note(
        model=model,
        fields=[
            with_front_gloss(front_text),
            with_back_gloss(back_text),
            "",
            "english-like",
            "foreign-like",
            front_visual,
            "",
            front_audio,
            back_audio,
            f"{card.idx:08d}",
        ],
        tags=[tag, "normal"],
        guid=guid,
    )


def build_deck(
    output_apkg: Path,
    deck_name: str,
    media_files: List[Path],
    cards: List[CardItem],
    tag: str,
    source_language: str,
) -> None:
    model_id = stable_int(f"model::{deck_name}::{tag}")
    model = create_note_model(model_id)

    decks: Dict[str, genanki.Deck] = {}

    for card in cards:
        target_deck_name = card.deck_name or deck_name

        if target_deck_name not in decks:
            decks[target_deck_name] = genanki.Deck(
                stable_deck_id(target_deck_name, tag),
                target_deck_name,
            )

        note = note_for_direction(
            model=model,
            card=card,
            tag=tag,
            source_language=source_language,
        )
        decks[target_deck_name].add_note(note)

    all_decks = list(decks.values())

    if len(all_decks) == 1:
        package = genanki.Package(all_decks[0])
    else:
        package = genanki.Package(all_decks)

    package.media_files = [str(p) for p in media_files]
    package.write_to_file(str(output_apkg))


def infer_language_code(language_name: str) -> Optional[str]:
    key = language_name.strip().lower()
    if key in {"en", "es", "it", "fr", "de", "fa", "ar", "zh", "ru", "ja", "tl", "ko", "id"}:
        return key
    return LANGUAGE_ALIASES.get(key)


def make_cards_from_map(
    aligned_pairs: List[Tuple[SubtitleLine, List[SubtitleLine]]],
    *,
    extras_by_index: Dict[int, dict],
    media_dir: Path,
    foreign_language_code: Optional[str],
    tts_backend: Optional[TTSBackend],
    split_every_n_cards: int,
    split_every_minutes: float,
    parent_deck_name: str,
    no_reverse: bool,
) -> Tuple[List[CardItem], List[Path], List[str]]:
    cards: List[CardItem] = []
    media_files: List[Path] = []
    warnings: List[str] = []

    for idx, (foreign_line, english_group) in enumerate(aligned_pairs):
        english_text = "\n".join(e.text_plain for e in english_group).strip() or "[No English text found]"
        foreign_text = foreign_line.text_plain
        extras = extras_by_index.get(idx, {})

        transliteration = extras.get("transliteration", "")
        ipa = extras.get("ipa", "")
        gloss = extras.get("gloss", "")
        english_gloss = extras.get("english_gloss", "")

        foreign_gloss_defs = build_gloss_definitions_html(gloss)
        english_gloss_defs = build_gloss_definitions_html(english_gloss)
        prev_english_texts = extras.get("prev_english_texts", [])
        next_english_texts = extras.get("next_english_texts", [])
        prev_foreign_texts = extras.get("prev_foreign_texts", [])
        next_foreign_texts = extras.get("next_foreign_texts", [])

        part_number = compute_part_number(
            card_index_zero_based=idx,
            clip_start_ms=foreign_line.start_ms,
            split_every_n_cards=split_every_n_cards,
            split_every_minutes=split_every_minutes,
        )

        split_enabled = bool(split_every_n_cards or split_every_minutes)

        normal_deck_name = make_directional_deck_name(
            parent_deck_name,
            part_number=part_number,
            split_enabled=split_enabled,
            foreign_language_code=foreign_language_code,
            reverse=False,
        )

        reverse_deck_name = make_directional_deck_name(
            parent_deck_name,
            part_number=part_number,
            split_enabled=split_enabled,
            foreign_language_code=foreign_language_code,
            reverse=True,
        )

        safe_prefix = f"map_{idx:05d}_{slugify(foreign_text[:32])}"

        front_image_path = media_dir / f"{safe_prefix}_front.png"
        back_image_path = media_dir / f"{safe_prefix}_back.png"

        front_image_ok = create_map_context_image(
            prev_texts=prev_english_texts,
            current_text=english_text,
            next_texts=next_english_texts,
            output_path=front_image_path,
        )

        back_image_ok = create_map_context_image(
            prev_texts=prev_foreign_texts,
            current_text=foreign_text,
            next_texts=next_foreign_texts,
            output_path=back_image_path,
        )

        front_image_name = ""
        back_image_name = ""

        if front_image_ok:
            media_files.append(front_image_path)
            front_image_name = front_image_path.name
        else:
            warnings.append(f"Map image skipped for card {idx} front side.")

        if back_image_ok:
            media_files.append(back_image_path)
            back_image_name = back_image_path.name
        else:
            warnings.append(f"Map image skipped for card {idx} back side.")

        tts_name = synthesize_tts_mp3(
            tts_backend=tts_backend,
            media_dir=media_dir,
            safe_prefix=safe_prefix,
            text=foreign_text,
            language_code=foreign_language_code,
            warnings=warnings,
            card_idx=idx,
        )

        if tts_name:
            media_files.append(media_dir / tts_name)

        cards.append(
            CardItem(
                idx=idx,
                start_ms=foreign_line.start_ms,
                end_ms=foreign_line.end_ms,
                english_text=english_text,
                foreign_text=foreign_text,
                foreign_transliteration=transliteration,
                foreign_ipa=ipa,
                foreign_gloss=gloss,
                front_gloss_defs=english_gloss_defs,
                back_gloss_defs=foreign_gloss_defs,
                thumbnail_name="",
                media_name="",
                tts_name=tts_name,
                deck_name=normal_deck_name,
                is_reverse=False,
                is_map_card=True,
                map_front_image_name=front_image_name,
                map_back_image_name=back_image_name,
            )
        )

        if not no_reverse:
            cards.append(
                CardItem(
                    idx=idx,
                    start_ms=foreign_line.start_ms,
                    end_ms=foreign_line.end_ms,
                    english_text=english_text,
                    foreign_text=foreign_text,
                    foreign_transliteration=transliteration,
                    foreign_ipa=ipa,
                    foreign_gloss=gloss,
                    front_gloss_defs=english_gloss_defs,
                    back_gloss_defs=foreign_gloss_defs,
                    thumbnail_name="",
                    media_name="",
                    tts_name=tts_name,
                    deck_name=reverse_deck_name,
                    is_reverse=True,
                    is_map_card=True,
                    map_front_image_name=back_image_name,
                    map_back_image_name=front_image_name,
                )
            )

    return cards, media_files, warnings


def make_cards(
    aligned_pairs: List[Tuple[SubtitleLine, List[SubtitleLine]]],
    *,
    media_path: Path,
    media_dir: Path,
    thumbnail_width: int,
    clip_height: int,
    clip_crf: int,
    source_language: str,
    foreign_language_code: Optional[str],
    video_duration_ms: Optional[int],
    tts_backend: Optional[TTSBackend],
    clean_tts_lines: Optional[List[SubtitleLine]],
    split_every_n_cards: int,
    split_every_minutes: float,
    parent_deck_name: str,
    no_reverse: bool,
) -> Tuple[List[CardItem], List[Path], List[str]]:
    cards: List[CardItem] = []
    media_files: List[Path] = []
    warnings: List[str] = []

    for idx, (foreign_line, english_group) in enumerate(aligned_pairs):
        english_text = "\n".join(e.text_plain for e in english_group).strip() or "[No English subtitle found]"
        foreign_text = foreign_line.text_plain
        clean_foreign_text = choose_clean_text_for_line(foreign_line, clean_tts_lines)
        foreign_added_text = extract_added_text_from_clean(foreign_text, clean_foreign_text)
        foreign_added_gloss_defs = build_gloss_definitions_html(foreign_added_text)

        part_number = compute_part_number(
            card_index_zero_based=idx,
            clip_start_ms=foreign_line.start_ms,
            split_every_n_cards=split_every_n_cards,
            split_every_minutes=split_every_minutes,
        )

        split_enabled = bool(split_every_n_cards or split_every_minutes)

        normal_deck_name = make_directional_deck_name(
            parent_deck_name,
            part_number=part_number,
            split_enabled=split_enabled,
            foreign_language_code=foreign_language_code,
            reverse=False,
        )

        reverse_deck_name = make_directional_deck_name(
            parent_deck_name,
            part_number=part_number,
            split_enabled=split_enabled,
            foreign_language_code=foreign_language_code,
            reverse=True,
        )

        safe_prefix = f"card_{idx:05d}_{slugify(foreign_text[:32])}"
        thumb_path = media_dir / f"{safe_prefix}.jpg"

        input_has_video = media_has_video(media_path)
        clip_ext = ".mp4" if input_has_video else ".mp3"
        clip_path = media_dir / f"{safe_prefix}{clip_ext}"
        tts_name = ""

        thumbnail_name = ""
        if input_has_video:
            thumb_at_ms = clamp_thumbnail_time_ms(
                midpoint_ms(foreign_line.start_ms, foreign_line.end_ms),
                video_duration_ms,
            )
            thumb_created = ffmpeg_thumbnail(media_path, thumb_path, thumb_at_ms, thumbnail_width)
            if thumb_created:
                media_files.append(thumb_path)
                thumbnail_name = thumb_path.name

        clip_start_ms = max(0, foreign_line.start_ms)
        requested_clip_end_ms = foreign_line.end_ms + 500

        if video_duration_ms is not None:
            clip_end_ms = min(requested_clip_end_ms, video_duration_ms)
        else:
            clip_end_ms = requested_clip_end_ms

        if clip_end_ms <= clip_start_ms:
            clip_end_ms = clip_start_ms + 250

        ffmpeg_clip(media_path, clip_path, clip_start_ms, clip_end_ms, clip_height, clip_crf)

        media_files.append(clip_path)

        if source_language == "english":
            if not foreign_language_code:
                warnings.append(
                    f"TTS skipped for card {idx}: could not infer language code from subtitle filename."
                )
            elif foreign_language_code not in SUPPORTED_LANGUAGES:
                warnings.append(
                    f"TTS skipped for card {idx}: language code '{foreign_language_code}' is not in the built-in voice map."
                )
            elif tts_backend is None:
                warnings.append(f"TTS skipped for card {idx}: no TTS backend configured.")
            else:
                raw_tts = media_dir / f"{safe_prefix}.aiff"
                mp3_tts = media_dir / f"{safe_prefix}.mp3"
                tts_source_text = choose_tts_text_for_line(foreign_line, clean_tts_lines)
                success = tts_backend.synthesize(tts_source_text, foreign_language_code, raw_tts)
                if success:
                    try:
                        transcode_audio_to_mp3(raw_tts, mp3_tts)
                        raw_tts.unlink(missing_ok=True)
                        media_files.append(mp3_tts)
                        tts_name = mp3_tts.name
                    except subprocess.CalledProcessError:
                        warnings.append(f"TTS generated but failed to transcode for card {idx}.")
                else:
                    warnings.append(
                        f"TTS skipped for card {idx}: local engine could not synthesize language '{foreign_language_code}'."
                    )

        cards.append(
            CardItem(
                idx=idx,
                start_ms=foreign_line.start_ms,
                end_ms=foreign_line.end_ms,
                english_text=english_text,
                foreign_text=foreign_text,
                clean_foreign_text=clean_foreign_text,
                foreign_added_text=foreign_added_text,
                front_gloss_defs="",
                back_gloss_defs=foreign_added_gloss_defs,
                foreign_transliteration="",
                foreign_ipa="",
                foreign_gloss="",
                thumbnail_name=thumbnail_name,
                media_name=clip_path.name,
                tts_name=tts_name,
                deck_name=normal_deck_name,
                is_reverse=False,
                is_map_card=False,
                map_front_image_name="",
                map_back_image_name="",
            )
        )

        if not no_reverse:
            cards.append(
                CardItem(
                    idx=idx,
                    start_ms=foreign_line.start_ms,
                    end_ms=foreign_line.end_ms,
                    english_text=english_text,
                    foreign_text=foreign_text,
                    clean_foreign_text=clean_foreign_text,
                    foreign_added_text=foreign_added_text,
                    front_gloss_defs="",
                    back_gloss_defs=foreign_added_gloss_defs,
                    foreign_transliteration="",
                    foreign_ipa="",
                    foreign_gloss="",
                    thumbnail_name=thumbnail_name,
                    media_name=clip_path.name,
                    tts_name=tts_name,
                    deck_name=reverse_deck_name,
                    is_reverse=True,
                    is_map_card=False,
                    map_front_image_name="",
                    map_back_image_name="",
                )
            )

    return cards, media_files, warnings


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate an Anki .apkg deck from a media file and two subtitle files.")
    parser.add_argument("directory", type=Path, help="Folder containing the video and subtitles")
    parser.add_argument("--deck-name", required=True, help="Name of the Anki deck to create")
    parser.add_argument("--output", type=Path, help="Output .apkg path (default: <deck-name>.apkg in the input directory)")
    parser.add_argument(
        "--source-language",
        choices=["foreign", "english"],
        default="foreign",
        help="Whether the source video's spoken language is foreign (default) or English",
    )
    parser.add_argument("--thumbnail-width", type=int, default=480)
    parser.add_argument("--clip-height", type=int, default=360)
    parser.add_argument("--clip-crf", type=int, default=24)
    parser.add_argument(
        "--tts-engine",
        choices=["auto", "say", "sherpa", "none"],
        default="auto",
        help="Local TTS backend for English-source mode",
    )
    parser.add_argument("--say-voice", help="Override macOS 'say' voice name")
    parser.add_argument("--sherpa-model-dir", help="Directory containing a local sherpa-onnx TTS model")
    parser.add_argument("--sherpa-tokens", help="Path to sherpa-onnx tokens.txt")
    parser.add_argument("--sherpa-data-dir", help="Optional sherpa-onnx data dir")
    parser.add_argument("--sherpa-dict-dir", help="Optional sherpa-onnx dict dir")
    parser.add_argument("--sherpa-rule-fsts", help="Optional sherpa-onnx rule_fsts path")
    parser.add_argument("--sherpa-rule-fars", help="Optional sherpa-onnx rule_fars path")
    parser.add_argument("--tag", default="video-dialogue")
    parser.add_argument(
        "--split-every-n-cards",
        type=int,
        default=0,
        help="If set to a positive integer, split the deck into subdecks of this many cards each.",
    )
    parser.add_argument(
        "--split-every-minutes",
        type=float,
        default=0,
        help="If set to a positive number, split the deck into subdecks by video time window in minutes (for example, 15 = one subdeck per 15 minutes).",
    )
    parser.add_argument(
        "--no-reverse",
        action="store_true",
        help="Only generate normal decks/cards and skip reverse decks.",
    )

    args = parser.parse_args(argv)
    if args.split_every_n_cards and args.split_every_n_cards < 1:
        parser.error("--split-every-n-cards must be greater than 0.")
    if args.split_every_minutes and args.split_every_minutes <= 0:
        parser.error("--split-every-minutes must be greater than 0.")
    if args.split_every_n_cards and args.split_every_minutes:
        parser.error("Use only one split mode: --split-every-n-cards or --split-every-minutes.")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = args.directory.expanduser().resolve()
    if not root.is_dir():
        raise DeckError(f"Not a directory: {root}")

    map_json_path = find_map_json_path(root)
    tts_backend = build_tts_backend(args)
    output_apkg = args.output or (root / f"{slugify(args.deck_name)}.apkg")

    if map_json_path is not None:
        media_files_in_dir = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
        subtitle_files_in_dir = [
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in SUB_EXTS and p.name.lower() != "map.json"
        ]

        if media_files_in_dir or subtitle_files_in_dir:
            raise DeckError(
                "When using Map.json mode, the directory must contain only Map.json and no media/subtitle input files."
            )

        ensure_tool("ffmpeg")

        payload = load_map_json(map_json_path)
        aligned_pairs, extras_by_index, foreign_language_code = build_map_aligned_pairs(payload)

        with tempfile.TemporaryDirectory(prefix="anki_map_deck_") as tmpdir:
            media_dir = Path(tmpdir)
            cards, media_files, warnings = make_cards_from_map(
                aligned_pairs,
                extras_by_index=extras_by_index,
                media_dir=media_dir,
                foreign_language_code=foreign_language_code,
                tts_backend=tts_backend,
                split_every_n_cards=args.split_every_n_cards,
                split_every_minutes=args.split_every_minutes,
                parent_deck_name=args.deck_name,
                no_reverse=args.no_reverse,
            )

            build_deck(
                output_apkg,
                args.deck_name,
                media_files,
                cards,
                args.tag,
                "foreign",
            )

        print(f"Created deck: {output_apkg}")
        print(f"Input: {map_json_path.name}")
        print(f"Aligned pairs: {len(aligned_pairs)}")
        print(f"Cards: {len(cards)}")
        print(f"Foreign language: {foreign_language_code or 'unknown'}")
        if warnings:
            print("\nWarnings:")
            for w in warnings[:50]:
                print(f"- {w}")
            if len(warnings) > 50:
                print(f"- ... and {len(warnings) - 50} more")
        return 0

    ensure_tool("ffmpeg")
    ensure_tool("ffprobe")

    media_path, english_sub_path, foreign_sub_path, foreign_language_name = find_input_files(root)
    foreign_language_code = infer_language_code(foreign_language_name)
    fps = probe_video_fps(media_path) if media_has_video(media_path) else None

    english_lines = load_subtitles(english_sub_path, fps=fps)
    foreign_lines = load_subtitles(foreign_sub_path, fps=fps)

    clean_tts_sub_path = find_clean_tts_subtitle_path(foreign_sub_path)
    clean_tts_lines: Optional[List[SubtitleLine]] = None
    if clean_tts_sub_path is not None:
        clean_tts_lines = load_subtitles(clean_tts_sub_path, fps=fps)

    video_duration_ms = probe_video_duration_ms(media_path) if media_has_video(media_path) else None

    if not english_lines:
        raise DeckError(f"No usable dialogue lines found in {english_sub_path.name}")
    if not foreign_lines:
        raise DeckError(f"No usable dialogue lines found in {foreign_sub_path.name}")

    aligned_pairs = align_subtitles(foreign_lines, english_lines)
    if not aligned_pairs:
        raise DeckError("No subtitle pairs could be aligned")

    with tempfile.TemporaryDirectory(prefix="anki_video_deck_") as tmpdir:
        media_dir = Path(tmpdir)
        cards, media_files, warnings = make_cards(
            aligned_pairs,
            media_path=media_path,
            media_dir=media_dir,
            thumbnail_width=args.thumbnail_width,
            clip_height=args.clip_height,
            clip_crf=args.clip_crf,
            source_language=args.source_language,
            foreign_language_code=foreign_language_code,
            tts_backend=tts_backend,
            clean_tts_lines=clean_tts_lines,
            video_duration_ms=video_duration_ms,
            split_every_n_cards=args.split_every_n_cards,
            split_every_minutes=args.split_every_minutes,
            parent_deck_name=args.deck_name,
            no_reverse=args.no_reverse,
        )

        build_deck(
            output_apkg,
            args.deck_name,
            media_files,
            cards,
            args.tag,
            args.source_language,
        )

    print(f"Created deck: {output_apkg}")
    print(f"Aligned pairs: {len(aligned_pairs)}")
    print(f"Cards: {len(cards)}")
    has_video, has_audio = probe_stream_types(media_path)
    media_kind = "video" if has_video else "audio"
    print(f"Media ({media_kind}): {media_path.name}")
    print(f"English subtitles: {english_sub_path.name}")
    print(f"Foreign subtitles: {foreign_sub_path.name}")
    if clean_tts_sub_path is not None:
        print(f"TTS subtitles: {clean_tts_sub_path.name}")
    else:
        print(f"TTS subtitles: {foreign_sub_path.name}")
    if foreign_language_code:
        print(f"Detected foreign language: {foreign_language_name} ({foreign_language_code})")
    else:
        print(f"Detected foreign language: {foreign_language_name} (code unknown)")

    if warnings:
        print("\nWarnings:")
        for w in warnings[:50]:
            print(f"- {w}")
        if len(warnings) > 50:
            print(f"- ... and {len(warnings) - 50} more")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DeckError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
