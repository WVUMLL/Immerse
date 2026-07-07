#!/usr/bin/env python3
"""
Generate an .srt subtitle file for a media file using local Whisper-family models on macOS.

Default path:
- Primary backend: mlx-whisper on Apple Silicon, using Whisper large-v3 for best accuracy
- Fallback backend: faster-whisper large-v3

Behavior:
- Accepts common video and audio file formats automatically
- Extracts mono 16 kHz WAV from the input media using ffmpeg
- Detects the primary spoken language by sampling multiple clips across the file
- Locks full transcription to that primary language
- Writes subtitles in the same directory as the input media, named like "Italian.srt"
- Produces subtitles only in the original spoken language (no translation)
- If --song is passed, isolates vocals with local demucs-mlx HTDemucs before transcription

Supported input examples:
- Video: .mp4, .mkv, .mov, .avi, .webm, .m4v
- Audio: .mp3, audio-only .mp4, .wav, .aac, .flac, .ogg, .wma, .alac, .pcm, .aiff

Tested design target:
- 2025 M4 MacBook Air, 16 GB unified memory

Usage:
    python autosrt.py /path/to/file.mp4
    python autosrt.py /path/to/file.mp3
    python autosrt.py /path/to/song.mp3 --song

Optional:
    python autosrt.py /path/to/file.mp4 --backend mlx
    python autosrt.py /path/to/file.mp3 --backend faster
    python autosrt.py /path/to/file.wav --model mlx-community/whisper-large-v3-mlx
    python autosrt.py /path/to/song.mp3 --song --backend mlx
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# -----------------------------
# Language name mapping
# -----------------------------

LANGUAGE_NAMES: Dict[str, str] = {
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "as": "Assamese",
    "az": "Azerbaijani",
    "ba": "Bashkir",
    "be": "Belarusian",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "bo": "Tibetan",
    "br": "Breton",
    "bs": "Bosnian",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fa": "Persian",
    "fi": "Finnish",
    "fo": "Faroese",
    "fr": "French",
    "gl": "Galician",
    "gu": "Gujarati",
    "ha": "Hausa",
    "haw": "Hawaiian",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "ht": "Haitian Creole",
    "hu": "Hungarian",
    "hy": "Armenian",
    "id": "Indonesian",
    "is": "Icelandic",
    "it": "Italian",
    "ja": "Japanese",
    "jw": "Javanese",
    "ka": "Georgian",
    "kk": "Kazakh",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "la": "Latin",
    "lb": "Luxembourgish",
    "ln": "Lingala",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mg": "Malagasy",
    "mi": "Maori",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "my": "Myanmar",
    "ne": "Nepali",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "oc": "Occitan",
    "pa": "Punjabi",
    "pl": "Polish",
    "ps": "Pashto",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sa": "Sanskrit",
    "sd": "Sindhi",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sn": "Shona",
    "so": "Somali",
    "sq": "Albanian",
    "sr": "Serbian",
    "su": "Sundanese",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "tg": "Tajik",
    "th": "Thai",
    "tk": "Turkmen",
    "tl": "Tagalog",
    "tr": "Turkish",
    "tt": "Tatar",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "yi": "Yiddish",
    "yo": "Yoruba",
    "zh": "Mandarin",
}

# Whisper commonly returns "zh" for Mandarin Chinese speech.
# You asked for "Mandarin" naming, so we map zh -> Mandarin.
PRIMARY_TARGET_NAMES = {
    "es": "Spanish",
    "it": "Italian",
    "fr": "French",
    "de": "German",
    "fa": "Persian",
    "ar": "Arabic",
    "zh": "Mandarin",
    "ru": "Russian",
    "ja": "Japanese",
    "tl": "Tagalog",
    "ko": "Korean",
    "id": "Indonesian",
}

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
ENGLISH_LANGUAGE_CODES = {"en"}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".aac", ".flac", ".ogg", ".wma", ".alac", ".pcm", ".aiff"
}

SUPPORTED_MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

@dataclass
class Segment:
    start: float
    end: float
    text: str
    language: Optional[str] = None
    language_probability: float = 0.0


@dataclass
class DetectionVote:
    language: str
    probability: float
    clip_start: float
    clip_duration: float


# -----------------------------
# ffmpeg / ffprobe helpers
# -----------------------------

def run_checked(cmd: Sequence[str], capture_output: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr}") from exc


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(
            f"Required tool '{name}' was not found in PATH. Install it first."
        )


def probe_duration_seconds(media_path: pathlib.Path) -> float:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(media_path),
    ]
    cp = run_checked(cmd, capture_output=True)
    data = json.loads(cp.stdout)
    duration = float(data["format"]["duration"])
    if duration <= 0:
        raise RuntimeError("Could not determine media duration.")
    return duration


def is_supported_media_file(path: pathlib.Path) -> bool:
    return path.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS


def extract_wav(
    input_path: pathlib.Path,
    output_wav: pathlib.Path,
    *,
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> None:
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(input_path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]

    cmd += [
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(output_wav),
    ]
    run_checked(cmd, capture_output=True)


# -----------------------------
# Demucs vocal isolation
# -----------------------------

def find_demucs_vocals_file(
    separated_root: pathlib.Path,
    model_name: str,
    input_stem: str,
) -> pathlib.Path:
    """
    Try the most likely output paths first, then fall back to a recursive search.
    This is more robust because different separator tools may keep or drop the
    original file extension in the output folder name.
    """
    candidate_paths = [
        separated_root / model_name / input_stem / "vocals.wav",
        separated_root / model_name / f"{input_stem}.wav" / "vocals.wav",
        separated_root / input_stem / "vocals.wav",
        separated_root / f"{input_stem}.wav" / "vocals.wav",
    ]

    for path in candidate_paths:
        if path.exists():
            return path

    matches = sorted(separated_root.rglob("vocals.wav"))
    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        print("Found multiple vocals.wav files; using the first one:")
        for match in matches:
            print(f"  {match}")
        return matches[0]

    raise RuntimeError(
        "demucs-mlx finished but could not find isolated vocals.wav anywhere under:\n"
        f"{separated_root}"
    )


def isolate_vocals_with_demucs(
    input_wav: pathlib.Path,
    workdir: pathlib.Path,
    *,
    model_name: str = "htdemucs",
) -> pathlib.Path:
    require_tool("ffmpeg")

    commands_to_try: List[List[str]] = []

    demucs_mlx_exe = shutil.which("demucs-mlx")
    if demucs_mlx_exe is not None:
        commands_to_try.append([
            demucs_mlx_exe,
            "-n", model_name,
            "-o", str(workdir),
            str(input_wav),
        ])

    commands_to_try.append([
        sys.executable,
        "-m",
        "demucs_mlx.cli",
        "-n", model_name,
        "-o", str(workdir),
        str(input_wav),
    ])

    last_error_text = ""

    print("Isolating vocals with demucs-mlx (htdemucs)...")

    for cmd in commands_to_try:
        print("Trying command:")
        print(" ".join(cmd))

        cp = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
        )

        if cp.returncode == 0:
            if cp.stdout.strip():
                print(cp.stdout.strip())
            if cp.stderr.strip():
                print(cp.stderr.strip())

            vocals_path = find_demucs_vocals_file(
                separated_root=workdir,
                model_name=model_name,
                input_stem=input_wav.stem,
            )
            print(f"Found isolated vocals file: {vocals_path}")
            return vocals_path

        error_text = (
            f"Command failed with exit code {cp.returncode}\n"
            f"STDOUT:\n{cp.stdout.strip() or '[empty]'}\n\n"
            f"STDERR:\n{cp.stderr.strip() or '[empty]'}"
        )
        print(error_text)
        last_error_text = error_text

    raise RuntimeError(
        "demucs-mlx vocal isolation failed after trying all launch methods.\n\n"
        f"{last_error_text}\n\n"
        "Make sure demucs-mlx is installed in this same Python environment with:\n"
        "python -m pip install -U demucs-mlx 'demucs-mlx[convert]'"
    )


# -----------------------------
# SRT formatting
# -----------------------------

def srt_timestamp(seconds: float) -> str:
    ms_total = max(0, int(round(seconds * 1000.0)))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clean_text(text: str) -> str:
    text = text.replace("-->", "→")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def segment_text_into_lines(text: str, max_chars: int = 42) -> List[str]:
    """
    Very simple subtitle line wrapping.
    Keeps 1–2 lines; long text is split greedily by words.
    """
    words = text.split()
    if not words:
        return []

    lines: List[str] = []
    current: List[str] = []
    current_len = 0

    for word in words:
        extra = 1 if current else 0
        if current_len + len(word) + extra <= max_chars:
            current.append(word)
            current_len += len(word) + extra
        else:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)

    if current:
        lines.append(" ".join(current))

    if len(lines) <= 2:
        return lines

    # Merge into two reasonably balanced lines if possible.
    mid = math.ceil(len(lines) / 2)
    return [" ".join(lines[:mid]), " ".join(lines[mid:])]


def write_srt(segments: Sequence[Segment], output_path: pathlib.Path) -> None:
    with output_path.open("w", encoding="utf-8") as f:
        idx = 1
        for seg in segments:
            text = clean_text(seg.text)
            if not text:
                continue
            start = max(0.0, seg.start)
            end = max(start + 0.05, seg.end)
            lines = segment_text_into_lines(text)
            if not lines:
                continue

            f.write(f"{idx}\n")
            f.write(f"{srt_timestamp(start)} --> {srt_timestamp(end)}\n")
            f.write("\n".join(lines))
            f.write("\n\n")
            idx += 1


# -----------------------------
# Backends
# -----------------------------

class BackendBase:
    name = "base"

    def detect_language(self, audio_path: pathlib.Path) -> Tuple[str, float]:
        raise NotImplementedError

    def transcribe(
        self,
        audio_path: pathlib.Path,
        language: Optional[str] = None,
        *,
        condition_on_previous_text: bool = True,
    ) -> List[Segment]:
        raise NotImplementedError


class MLXWhisperBackend(BackendBase):
    name = "mlx"

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        try:
            import mlx_whisper  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mlx-whisper is not installed. Install it with: pip install -U mlx-whisper"
            ) from exc
        self.mlx_whisper = mlx_whisper

    def detect_language(self, audio_path: pathlib.Path) -> Tuple[str, float]:
        # We ask for word_timestamps=False to minimize work during detection.
        result = self.mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=self.model_name,
            task="transcribe",
            word_timestamps=False,
            verbose=False,
            temperature=0.0,
            condition_on_previous_text=False,
        )

        # Different versions may expose slightly different fields.
        language = result.get("language")
        probability = result.get("language_probability")

        if not language:
            raise RuntimeError("MLX backend did not return a detected language.")
        if probability is None:
            probability = 0.0

        return str(language), float(probability)

    def transcribe(
        self,
        audio_path: pathlib.Path,
        language: Optional[str] = None,
        *,
        condition_on_previous_text: bool = True,
    ) -> List[Segment]:
        kwargs = {
            "audio": str(audio_path),
            "path_or_hf_repo": self.model_name,
            "task": "transcribe",
            "word_timestamps": True,
            "verbose": False,
            "temperature": 0.0,
            "condition_on_previous_text": condition_on_previous_text,
            "compression_ratio_threshold": 2.4,
            "logprob_threshold": -1.0,
            "no_speech_threshold": 0.6,
            "hallucination_silence_threshold": 1.5,
        }
        if language is not None:
            kwargs["language"] = language

        result = self.mlx_whisper.transcribe(**kwargs)

        raw_segments = result.get("segments", [])
        detected_language = result.get("language")
        detected_probability = result.get("language_probability")
        if detected_probability is None:
            detected_probability = 0.0

        segments: List[Segment] = []
        for seg in raw_segments:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", start))
            text = str(seg.get("text", "")).strip()
            if text:
                segments.append(
                    Segment(
                        start=start,
                        end=end,
                        text=text,
                        language=str(detected_language) if detected_language else None,
                        language_probability=float(detected_probability),
                    )
                )
        return segments


class FasterWhisperBackend(BackendBase):
    name = "faster"

    def __init__(self, model_name: str = "large-v3") -> None:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed. Install it with: pip install -U faster-whisper"
            ) from exc

        # On macOS Apple Silicon, faster-whisper usually runs on CPU via CTranslate2.
        # This is intended as a fallback, not the primary optimized path.
        self.WhisperModel = WhisperModel
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def detect_language(self, audio_path: pathlib.Path) -> Tuple[str, float]:
        segments, info = self.model.transcribe(
            str(audio_path),
            beam_size=5,
            task="transcribe",
            word_timestamps=False,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        # Force evaluation
        _ = list(segments)
        return str(info.language), float(info.language_probability)

    def transcribe(
        self,
        audio_path: pathlib.Path,
        language: Optional[str] = None,
        *,
        condition_on_previous_text: bool = True,
    ) -> List[Segment]:
        kwargs = {
            "audio": str(audio_path),
            "beam_size": 5,
            "task": "transcribe",
            "word_timestamps": True,
            "condition_on_previous_text": condition_on_previous_text,
            "vad_filter": True,
        }
        if language is not None:
            kwargs["language"] = language

        segments_iter, info = self.model.transcribe(**kwargs)

        segments: List[Segment] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if text:
                segments.append(
                    Segment(
                        start=float(seg.start),
                        end=float(seg.end),
                        text=text,
                        language=str(info.language) if getattr(info, "language", None) else None,
                        language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
                    )
                )
        return segments


# -----------------------------
# Primary language detection
# -----------------------------

def choose_clip_plan(total_duration: float) -> List[Tuple[float, float]]:
    """
    Return (start, duration) samples spread across the file.
    Keeps clips away from exact edges when possible.
    """
    clip_len = 30.0
    if total_duration <= 45:
        return [(0.0, total_duration)]

    positions = [0.10, 0.25, 0.50, 0.75, 0.90]
    clips: List[Tuple[float, float]] = []

    for p in positions:
        center = total_duration * p
        start = max(0.0, min(center - clip_len / 2.0, total_duration - clip_len))
        duration = min(clip_len, total_duration - start)
        if duration >= 8.0:
            clips.append((start, duration))

    deduped: List[Tuple[float, float]] = []
    for start, dur in clips:
        if not deduped or abs(start - deduped[-1][0]) > 3.0:
            deduped.append((start, dur))

    return deduped


def detect_primary_language(
    backend: BackendBase,
    input_audio_source: pathlib.Path,
    workdir: pathlib.Path,
) -> Tuple[str, List[DetectionVote]]:
    duration = probe_duration_seconds(input_audio_source)
    plan = choose_clip_plan(duration)

    votes: List[DetectionVote] = []

    for idx, (start, clip_dur) in enumerate(plan, start=1):
        clip_wav = workdir / f"lang_probe_{idx}.wav"
        extract_wav(input_audio_source, clip_wav, start=start, duration=clip_dur)

        lang, prob = backend.detect_language(clip_wav)
        votes.append(
            DetectionVote(
                language=lang,
                probability=prob,
                clip_start=start,
                clip_duration=clip_dur,
            )
        )

    if not votes:
        raise RuntimeError("Could not obtain any language-detection samples.")

    score_by_lang: Dict[str, float] = collections.defaultdict(float)

    for vote in votes:
        weight = vote.probability if vote.probability > 0 else 1.0
        score_by_lang[vote.language] += weight * vote.clip_duration

    primary = max(score_by_lang.items(), key=lambda kv: kv[1])[0]
    return primary, votes


# -----------------------------
# Transcript repair and language filtering
# -----------------------------

def normalize_for_repetition(text: str) -> str:
    text = clean_text(text).lower()
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def duration_of(seg: Segment) -> float:
    return max(0.0, seg.end - seg.start)


def looks_like_repetition_cluster(texts: Sequence[str]) -> bool:
    normalized = [normalize_for_repetition(t) for t in texts if normalize_for_repetition(t)]
    if len(normalized) < 3:
        return False

    counts = collections.Counter(normalized)
    most_common_text, most_common_count = counts.most_common(1)[0]
    if most_common_count >= 3:
        return True

    short_unique = set(normalized)
    if len(short_unique) <= 2 and all(len(t.split()) <= 6 for t in short_unique):
        return True

    return False


def find_repetition_drift_start(
    segments: Sequence[Segment],
    *,
    window_size: int = 5,
) -> Optional[int]:
    if len(segments) < window_size:
        return None

    for i in range(0, len(segments) - window_size + 1):
        window = segments[i:i + window_size]
        texts = [seg.text for seg in window]
        if not looks_like_repetition_cluster(texts):
            continue

        repeated_duration = sum(duration_of(seg) for seg in window)
        if repeated_duration < 2.0:
            continue

        return i

    return None


def repetition_score(segments: Sequence[Segment]) -> int:
    if not segments:
        return 0

    score = 0
    i = 0
    while i < len(segments):
        j = i + 1
        current = normalize_for_repetition(segments[i].text)
        if not current:
            i += 1
            continue

        while j < len(segments):
            nxt = normalize_for_repetition(segments[j].text)
            if nxt == current:
                j += 1
            else:
                break

        run_len = j - i
        if run_len >= 3:
            score += run_len

        i = j

    return score


def extract_suffix_wav(
    input_audio_source: pathlib.Path,
    output_wav: pathlib.Path,
    *,
    start: float,
) -> None:
    extract_wav(input_audio_source, output_wav, start=start, duration=None)


def transcribe_suffix(
    backend: BackendBase,
    input_audio_source: pathlib.Path,
    workdir: pathlib.Path,
    *,
    start_time: float,
    language: Optional[str] = None,
    condition_on_previous_text: bool = False,
) -> List[Segment]:
    suffix_wav = workdir / f"suffix_{int(start_time * 1000)}.wav"
    extract_suffix_wav(input_audio_source, suffix_wav, start=start_time)

    suffix_segments = backend.transcribe(
        suffix_wav,
        language=language,
        condition_on_previous_text=condition_on_previous_text,
    )

    shifted: List[Segment] = []
    for seg in suffix_segments:
        shifted.append(
            Segment(
                start=seg.start + start_time,
                end=seg.end + start_time,
                text=seg.text,
                language=seg.language,
                language_probability=seg.language_probability,
            )
        )
    return shifted


def suffix_materially_improves_repetition(
    old_suffix: Sequence[Segment],
    new_suffix: Sequence[Segment],
) -> bool:
    old_score = repetition_score(old_suffix)
    new_score = repetition_score(new_suffix)

    if new_score >= old_score:
        return False

    return True


def repair_repetition_drift(
    backend: BackendBase,
    input_audio_source: pathlib.Path,
    workdir: pathlib.Path,
    segments: List[Segment],
    *,
    language: Optional[str] = None,
    max_repairs: int = 8,
) -> List[Segment]:
    repaired = list(segments)

    for repair_index in range(max_repairs):
        drift_idx = find_repetition_drift_start(repaired)
        if drift_idx is None:
            return repaired

        drift_start_time = repaired[drift_idx].start
        prefix = repaired[:drift_idx]
        old_suffix = repaired[drift_idx:]

        print(
            f"Potential repetition drift detected at {drift_start_time:.3f}s; "
            f"attempting suffix re-transcription #{repair_index + 1}..."
        )

        new_suffix = transcribe_suffix(
            backend,
            input_audio_source,
            workdir,
            start_time=drift_start_time,
            language=language,
            condition_on_previous_text=False,
        )

        if not new_suffix:
            print("Suffix re-transcription returned no segments; keeping original suffix.")
            return repaired

        if not suffix_materially_improves_repetition(old_suffix, new_suffix):
            print("Suffix re-transcription did not materially reduce repetition; keeping original suffix.")
            return repaired

        repaired = prefix + new_suffix

    return repaired


def detect_segment_language(
    backend: BackendBase,
    input_audio_source: pathlib.Path,
    workdir: pathlib.Path,
    seg: Segment,
    *,
    min_probe_duration: float = 1.2,
) -> Tuple[Optional[str], float]:
    seg_duration = max(0.0, seg.end - seg.start)
    if seg_duration <= 0.0:
        return None, 0.0

    probe_duration = max(min_probe_duration, seg_duration)
    probe_wav = workdir / f"langseg_{int(seg.start * 1000)}_{int(seg.end * 1000)}.wav"
    extract_wav(
        input_audio_source,
        probe_wav,
        start=seg.start,
        duration=probe_duration,
    )

    try:
        lang, prob = backend.detect_language(probe_wav)
        return lang, prob
    except Exception:
        return None, 0.0


def annotate_segments_with_language(
    backend: BackendBase,
    input_audio_source: pathlib.Path,
    workdir: pathlib.Path,
    segments: Sequence[Segment],
) -> List[Segment]:
    annotated: List[Segment] = []
    for idx, seg in enumerate(segments, start=1):
        lang, prob = detect_segment_language(backend, input_audio_source, workdir, seg)
        annotated.append(
            Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
                language=lang,
                language_probability=prob,
            )
        )
        if idx % 50 == 0:
            print(f"Language-labeled {idx} subtitle segments...")
    return annotated


def choose_primary_language_by_duration(segments: Sequence[Segment]) -> str:
    durations: Dict[str, float] = collections.defaultdict(float)

    for seg in segments:
        if not seg.language:
            continue
        durations[seg.language] += duration_of(seg)

    if not durations:
        raise RuntimeError("Could not determine a primary language from subtitle segments.")

    return max(durations.items(), key=lambda kv: kv[1])[0]


def filter_segments_by_english_rule(
    segments: Sequence[Segment],
    primary_language: str,
) -> List[Segment]:
    # If the primary language is English, keep everything.
    if primary_language in ENGLISH_LANGUAGE_CODES:
        return list(segments)

    # If the primary language is not English, remove only English segments.
    filtered: List[Segment] = []
    for seg in segments:
        if seg.language in ENGLISH_LANGUAGE_CODES:
            continue
        filtered.append(seg)

    return filtered


# -----------------------------
# Output naming
# -----------------------------

def language_display_name(lang_code: str) -> str:
    lang_code = lang_code.lower()
    if lang_code in PRIMARY_TARGET_NAMES:
        return PRIMARY_TARGET_NAMES[lang_code]
    return LANGUAGE_NAMES.get(lang_code, lang_code.title())


def normalize_language_argument(value: str) -> str:
    """
    Turn a user-supplied language (a Whisper code like 'fr' or a name like
    'French', in any capitalization) into the Whisper language code.
    """
    cleaned = value.strip().lower()

    if cleaned in LANGUAGE_NAMES:
        return cleaned

    aliases = {"chinese": "zh"}
    if cleaned in aliases:
        return aliases[cleaned]

    for code, name in LANGUAGE_NAMES.items():
        if name.lower() == cleaned:
            return code

    raise RuntimeError(
        f"Unknown language: {value!r}. Use a Whisper code such as "
        "'fr', 'es', 'de', 'ja', or a full name such as "
        "'French', 'Spanish', 'German', 'Japanese'."
    )


def sanitize_filename(name: str) -> str:
    clean = SAFE_FILENAME_RE.sub("", name).strip()
    return clean or "Subtitles"


# -----------------------------
# Main pipeline
# -----------------------------

def transcribe_media(
    input_media: pathlib.Path,
    backend_name: str,
    model_name: str,
    song_mode: bool = False,
    forced_language: Optional[str] = None,
) -> pathlib.Path:
    require_tool("ffmpeg")
    require_tool("ffprobe")

    if not input_media.exists():
        raise RuntimeError(f"Input file does not exist: {input_media}")

    if not is_supported_media_file(input_media):
        supported = ", ".join(sorted(SUPPORTED_MEDIA_EXTENSIONS))
        raise RuntimeError(
            f"Unsupported file type: {input_media.suffix or '[no extension]'}. "
            f"Supported formats: {supported}"
        )

    if backend_name == "mlx":
        backend: BackendBase = MLXWhisperBackend(model_name=model_name)
    elif backend_name == "faster":
        backend = FasterWhisperBackend(model_name="large-v3")
    elif backend_name == "auto":
        try:
            backend = MLXWhisperBackend(model_name=model_name)
        except Exception:
            backend = FasterWhisperBackend(model_name="large-v3")
    else:
        raise RuntimeError(f"Unsupported backend: {backend_name}")

    with tempfile.TemporaryDirectory(prefix="autosrt_") as tmpdir:
        workdir = pathlib.Path(tmpdir)

        full_wav = workdir / "full_audio.wav"
        extract_wav(input_media, full_wav)

        transcription_wav = full_wav
        if song_mode:
            vocals_wav = isolate_vocals_with_demucs(full_wav, workdir, model_name="htdemucs")
            transcription_wav = vocals_wav
            print(f"Using isolated vocals for transcription: {transcription_wav}")

        if forced_language is not None:
            primary_lang = forced_language
            print(
                "Skipping language auto-detection; using language from "
                f"--language: {primary_lang} ({language_display_name(primary_lang)})"
            )
        else:
            print("Detecting primary language before transcription...")
            primary_lang, detection_votes = detect_primary_language(
                backend,
                transcription_wav,
                workdir,
            )

            print(f"Detected primary language: {primary_lang} ({language_display_name(primary_lang)})")
            for vote in detection_votes:
                print(
                    f"  sample at {vote.clip_start:.1f}s: "
                    f"{vote.language} probability={vote.probability:.3f}"
                )

        print("Running initial full transcription with primary language locked...")
        try:
            raw_segments = backend.transcribe(
                transcription_wav,
                language=primary_lang,
                condition_on_previous_text=True,
            )
        except Exception as exc:
            if backend.name == "mlx":
                print("MLX transcription failed; falling back to faster-whisper...", file=sys.stderr)
                fallback = FasterWhisperBackend(model_name="large-v3")
                raw_segments = fallback.transcribe(
                    full_wav,
                    language=primary_lang,
                    condition_on_previous_text=True,
                )
                backend = fallback
            else:
                raise RuntimeError(f"Transcription failed: {exc}") from exc

        if not raw_segments:
            raise RuntimeError("No subtitle segments were produced.")

        print("Repairing repetition drift if needed...")
        repaired_segments = repair_repetition_drift(
            backend,
            transcription_wav,
            workdir,
            raw_segments,
            language=primary_lang,
        )

        print("Detecting language for each subtitle segment...")
        annotated_segments = annotate_segments_with_language(
            backend,
            transcription_wav,
            workdir,
            repaired_segments,
        )

        print(f"Primary language used for output: {primary_lang} ({language_display_name(primary_lang)})")
        if primary_lang in ENGLISH_LANGUAGE_CODES:
            print("Primary language is English; keeping all subtitle segments.")
        else:
            print("Primary language is not English; removing only English subtitle segments.")

        filtered_segments = filter_segments_by_english_rule(
            annotated_segments,
            primary_lang,
        )

        if not filtered_segments:
            raise RuntimeError("All subtitle segments were filtered out.")

        lang_name = sanitize_filename(language_display_name(primary_lang))
        output_path = input_media.parent / f"{lang_name}.srt"
        write_srt(filtered_segments, output_path)

        return output_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate primary-language .srt subtitles from a media file."
    )
    parser.add_argument(
        "media",
        help="Path to the media file (video or audio)"
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "mlx", "faster"],
        default="auto",
        help="Transcription backend. Default: auto",
    )
    parser.add_argument(
        "--model",
        default="mlx-community/whisper-large-v3-mlx",
        help=(
            "MLX model name to use when backend is auto/mlx. "
            "Default: mlx-community/whisper-large-v3-mlx"
        ),
    )
    parser.add_argument(
        "--song",
        action="store_true",
        help=(
            "If set, isolate vocals from the extracted WAV using demucs-mlx "
            "before transcription."
        ),
    )
    parser.add_argument(
        "--language",
        default=None,
        help=(
            "Skip auto-detection and lock transcription to this language. "
            "Accepts a Whisper code like 'fr' or a name like 'French'. "
            "Default: detect the language automatically."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_media = pathlib.Path(args.media).expanduser().resolve()

    try:
        forced_language = None
        if args.language is not None:
            forced_language = normalize_language_argument(args.language)

        output = transcribe_media(
            input_media=input_media,
            backend_name=args.backend,
            model_name=args.model,
            song_mode=args.song,
            forced_language=forced_language,
        )
        print(f"\nWrote subtitles: {output}")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
