# Immerse
A toolkit for turning any media resource into a language-learning Anki deck!
- Compatible with videos (recordings, clips, TV, movies), audio (recordings, podcasts, music), and text (books, articles, pdfs, plain text)
- Decks designed to promote reading/writing and listening/speaking
- Generates in-context translations in both directions, English → foreign and foreign → English

Developed by Armin Salehipour for WVU Medical Language Learners.

<img src="Logo.png" alt="drawing" width="250"/>

**TODO:**
- [ ] Add demo images to Github (video clip, song, text)
- [ ] Package for easy layman use
	- [ ] Meta-program that chains Subtitle Generation → Claude API Handling → Anki Generation
	- [ ] UI
	- [ ] Package into binary
- [ ] Vocab mode: Cards for individual lexemes (words/phrases)
	- [ ] Option: Top X number of words
	- [ ] Option: Filter words based on most default most common vocab list, other text source, or both

# How to Install

⚠️ This program is intended for WVU HSC students and assumes your device is a MacBook Air ⚠️

These instructions are also intended for those with little-to-no experience using Terminal or working with python.
If you have experience with these things, feel free to deviate from the instructions as you see fit!

**0a - Download Files**

Click the green button on this page that says "Code" and select "Download ZIP".
Unzip "Immerse-main.zip" and move the "Immerse-main" folder wherever you want to keep it.

From now on, the instructions will refer to this folder with the placeholder `/path/to/Immerse-main`.
Files inside that folder follow that path name (e.g., `/path/to/Immerse-main/requirements.txt`).
Whenever you see these placeholders, replace them with the actual path to the folder/file.

TIP: You can drag a folder or file into Terminal to automatically paste its full path.

**0b - Open the "Terminal" app (already installed on your Mac)**

**1a - Install Homebrew**

Copy and paste this into Terminal
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```
Press Enter to run the command.

Follow on-screen instructions.
You may be asked to enter your Mac password. When you type it, you won't see any visual feedback. Just press enter again after you're done typing it.

At the end, Homebrew will show a command like this. Copy and run it
```
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```
This step allows your system to recognize Homebrew commands.

Lastly, verify it installed with this
```
brew --version
```
It should show you the Homebrew version you have installed

**1b - Install Python3**
```
brew install python
```

Verify your python version
```
python3 --version
```

Verify your pip version
```
pip3 --version
```

Optional: This lets you type `python` and `pip` (like in our instructions below) instead of `python3` and `pip3`.
```
echo 'alias python=python3' >> ~/.zshrc
echo 'alias pip=pip3' >> ~/.zshrc
source ~/.zshrc
```
If you choose not to, or this step is giving you problems, just enter `python3` whenever our instructions say `python` and `pip3` whenever our instructions say `pip`.
If anything breaks later, you can remove these lines from ~/.zshrc.

**2 - Install required system tools (if necessary)**
```
xcode-select --install
sudo xcodebuild -license accept
brew install ffmpeg
```
If a popup appears, click "Install".

**3 - Recommended, but optional: Create an environment**
```
python -m venv .anki_immersion_env
```
This creates a self-contained environment that doesn't interfere with other python projects.

**4a - If you created an environment, this command activates that environment.**
**If you didn't create an environment, skip to 4b.**
```
source .anki_immersion_env/bin/activate
```
This activates the environment. You should see its name appear at the start of your Terminal line.
You should now see something like:
```
(.anki_immersion_env) your-name@MacBook-Air ...
```

If you close Terminal later, you must run this again before using the program.

**4b - Install python dependencies**
```
pip install -r /path/to/Immerse-main/requirements.txt
```

**4c - To complete song support**
```
python -m pip install -U "demucs-mlx[convert]"
```

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

⚠️ This is the most time-consuming step! If you don't want to wait hours to process longer media (shows, movies, etc.), we recommend splitting them into smaller clips and making decks in parts. ⚠️

### Basic usage

#### 1. If you made an environment, activate it. Otherwise, skip to step 2.
```
source .anki_immersion_env/bin/activate
```

#### 2. Execution
```
python /path/to/Immerse-main/autosrt.py [PUT_MEDIA_PATH_HERE] --model mlx-community/whisper-large-v3-mlx
```

##### Optional Arguments
```
# Add this to the end if you're doing song lyrics
--song
```

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

> For map.json files, just remove "subtitles". For other text files, replace "subtitles" with "text file".
> 
> This can also be modified to filter dialogue for a certain proficiency level (e.g., CEFR B1).

#### Songs: .LRC → .SRT

```
Generate subtitles in .srt format using the provided lyrics that are in .lrc format. For each lyric, convert its timestamp to the equivalent starting timestamp in .srt format and set the ending .srt timestamp for that lyric to the timestamp of the next lyric. If the final lyrics are not followed by another timestamp, set its .srt ending timestamp to "99:99:99,000". Be sure to retain the the original, unchanged, lryic content in the subtitles.
```

## Anki Generation

### Basic usage

#### 1. If you made an environment, activate it. Otherwise, skip to step 2.
```
source .anki_immersion_env/bin/activate
```

#### 2. Execution
```
python /path/to/Immerse-main/anki_media_deck.py [PUT_FOLDER_PATH_HERE] --deck-name "[DECK_NAME_HERE]"
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

### Optional sherpa-onnx usage (Necessary for Persian (Farsi), Arabic, Tagalog, and Indonesian Text-to-Speech)

**TODO:**
- [ ] Language model installation instructions
- [ ] Links for Persian (Farsi), Arabic, Tagalog, and Indonesian models

```
# Usage Example
python /path/to/Immerse-main/anki_media_deck.py [FOLDER_PATH] --deck-name "[DECK_NAME]" --source-video-language english --tts-engine sherpa --sherpa-model-dir [MODEL_PATH] --split-every-n-cards 10
```
You will need to manually download the model for your target language and send it.
If this is confusing or overwhelming, just ignore it. It's not necessary.
