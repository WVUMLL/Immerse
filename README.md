# Immerse
A toolkit for turning any media resource into a language-learning Anki deck!
- Compatible with videos (recordings, clips, TV, movies), audio (recordings, podcasts, music), and text (books, articles, pdfs, plaintext)
- Decks designed to promote reading/writing and listening/speaking
- Generates both EN → Foreign and Foreign → EN

Developed by Armin Salehipour for WVU Medical Language Learners.

<img src="Logo.png" alt="drawing" width="250"/>

**TODO:**
- [ ] Package for easy layman use
	- [ ] Merge programs into one environment → Updated Requirements.txt → Add to Git with full installation instructions
	- [ ] Meta-program that chains Subtitle Generation → Claude API Handling → Anki Generation
	- [ ] UI
	- [ ] Package into binary

# How to Use

## Program Workflow

**Inputs:** Audiovisual Media (supplements: .LRC → Assume song, English subs), Single text, Two texts

Media → Generate Subtitles (--song?) → [LLM] Context Filter? → [LLM] Translate Subtitles → [LLM] Gloss (rename original to clean)? → Anki (--deck-name, --source-language english?, --split?)
…
Generate w/ --song ⇒ Go straight to Translate

Media + .LRC → [LLM] Convert to .SRT → [LLM] Translate Subtitles → ...

Single Text File → [LLM] Translate → [LLM] Map.json → [LLM] Gloss Map? → Anki

Two Text Files → [LLM] Map.json → ...

## Subtitle Generation

### Installation

#### HTDemucs
```
python3 -m pip install -U pip setuptools wheel
python3 -m pip install -U torch torchvision torchaudio
python3 -m pip install -U demucs
python -m pip install -U demucs-mlx
python -m pip install -U "demucs-mlx[convert]"
```

### Basic usage

#### 1a. Set environment
```
whisperxenv
```

#### 2. Execution
```
python /path/to/autosrt.py [MEDIA_PATH] --model mlx-community/whisper-large-v3-mlx
```

##### Optional Arguments
```
# Isolate lyrics
--song
```

---
## LLM Prompts (with Claude)

### Map.json Construction

#### Mapping Pre-Existing Translations

Map equivalent text between the provided translation files to generate a JSON file with the following fixed schema:
```
{
  "metadata": {
    "source_language": "en",
    "target_language": "es",
    "alignment_type": "segment",
    "version": "1.0"
  },
  "segments": [
    {
      "id": "seg_001",
      "source": {
        "text": "The quick brown fox jumps over the lazy dog.",
        "start_char": 0,
        "end_char": 44
      },
      "target": {
        "text": "El rápido zorro marrón salta sobre el perro perezoso.",
        "start_char": 0,
        "end_char": 62
      },
      "alignment": {
        "type": "1-1",
        "confidence": 0.98
      }
    }
  ]
}
```
A segment is a mapping of each of the source's independent clauses, in order, to their counterpart in the target.
Each segment must include all properties represented in this schema.
Each segment's `alignment.type` can be `"1-1"` for one-to-one sentence matching, `"1-n"` for one-to-many, `"n-1"` for many-to-one, and `"partial"` for partial matches without a more exact match.
Each independent clause of the source should be accounted for as a segment. If there is no match from the target, generate a likely translation matching the target's language given the context of the text for the `target.text`, with `target.start_char` and `target.end_char` set to `-1` and `alignment.type` set to `missing`.
Only map the main content of the file. Do not map any format byproducts or artifacts (page numbers, redundant chapter titles, citations, headers and footers, timestamps, etc.).
The source is the foreign language file and the target is the English file.

**Option for Longer Texts:** Only map the [n] chapter, "[TITLE]", of the source to its corresponding English chapter.

#### Optional: Add Leipzig gloss & transliteration to foreign mapped segments

```
In this Map.json file, for each element in `segments`, add Leipzig gloss of the element's `source.text` as a string assigned to `source.gloss`. If `source.text` is not in Latin script, also include a transliteration as a string assigned to `source.transliteration`. Generate an updated file of the same format. Be sure to keep all of the original file content, just with the Leipzig gloss and potential transliteration additions.
```

### Subtitle Modifications

#### Translate Foreign → English

```
You are an expert in natural language translation. Provide an English translation of the following [LANGUAGE] subtitles and generate the same format subtitle file with the same time intervals using the English dialogue. Use translations most natural to the context of the dialogue in the subtitles.
```

> For translating English → Foreign:
> 	1. Replace "English" with the foreign language name
> 	2. Replace "\[LANGUAGE]" with "English"

**TODO:**
- [ ] If there is ambiguity that lends itself to fundamentally different likely interpretations, even within context, provide each possible translation separated by new lines with "OR" as a line between them.

#### Optional: Add Leipzig gloss & transliteration to foreign subtitles

```
Insert Leipzig gloss into the provided [LANGUAGE] subtitles file such that each subtitle interval has the Leipzig gloss on a line beneath the original subtitles. If the subtitles are not in Latin script, include a transliteration on a line beneath the original text, followed by the Leipzig gloss line. Generate an updated subtitles file of the same format. Be sure to keep the same time intervals and original subtitle content.
```

**TODO:**
- [ ] Explicate marking any uncertainties or potential artifacts with (?)

#### Optional: Context filtering

```
Filter the provided subtitles file to only retain dialgoue that could be heard or said in [CONTEXT]. Generate an updated subtitles file of the same format. Be sure to keep the same time intervals and original subtitle content for the dialogue that is not removed.
```

> For map.json files, just remove "subtitles". For other text files, replace "subtitles" with "text file"
#### Songs: .LRC → .SRT

```
Generate subtitles in .srt format using the provided lyrics that are in .lrc format. For each lyric, convert its timestamp to the equivalent starting timestamp in .srt format and set the ending .srt timestamp for that lyric to the timestamp of the next lyric. If the final lyrics are not followed by another timestamp, set its .srt ending timestamp to "99:99:99,000". Be sure to retain the the original, unchanged, lryic content in the subtitles.
```

---
## Anki Generation

**TODO:**
- [ ] Map file support
	- [ ] Maybe include screenshot generated from text to show preceding and following sentence for context.

### Installation

```
python3 -m venv .anki_immersion_env
source .anki_immersion_env/bin/activate
pip install -r /path/to/requirements.txt

# required system tools (if necessary)
xcode-select --install
sudo xcodebuild -license accept
brew install ffmpeg
```

### Basic usage

#### 1. Set environment
```
source ~/.anki_immersion_env/bin/activate
```

#### 2. Execution
```
python /path/to/anki_media_deck.py [FOLDER_PATH] --deck-name "[NAME]"
```

##### Optional Arguments
```
# For English Sources (Use Foreign TTS)
--source-language english

# Example: Subdecks for every 10 cards
--split-every-n-cards 10

# Example: Subdecks for every 15 minutes
--split-every-minutes 15

# Only generate English → Foreign
--no-reverse
```

### Optional sherpa-onnx usage

```
# Installation
python -m pip install --upgrade pip setuptools wheel
pip install cmake
pip install sherpa-onnx

# Usage Example
python /path/to/anki_media_deck.py [FOLDER_PATH] --deck-name "[NAME]" --source-video-language english --tts-engine sherpa --sherpa-model-dir [MODEL_PATH] --split-every-n-cards 10
```
You will need to manually download the model for your target language and send it
