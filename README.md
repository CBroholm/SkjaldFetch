# SkjaldFetch

CLI tool for downloading and transcribing podcasts and videos.
Saves the result as a Markdown file — and optionally drops it in a Bertha inbox.

## Features

- **Any URL** — YouTube, podcast RSS feeds, direct MP3 links, and more (via yt-dlp)
- **Spotify** — auto-resolves Spotify episode URLs to their public RSS feed; no DRM workarounds needed
- **Speaker diarization** — powered by AssemblyAI Universal model
- **Bertha integration** — saves to a configurable inbox folder for daily pickup

## Setup

```bash
pip install yt-dlp assemblyai python-dotenv requests
cp .env.example .env
# Fill in ASSEMBLYAI_API_KEY (and optionally OUTPUT_DIR + BERTHA_INBOX) in .env
```

## Usage

```bash
# Spotify episode (auto-resolved via RSS)
python skaldfetch.py "https://open.spotify.com/episode/..."

# YouTube video
python skaldfetch.py "https://www.youtube.com/watch?v=..."

# Direct podcast MP3
python skaldfetch.py "https://feeds.example.com/episode.mp3"

# Local file
python skaldfetch.py "C:\Downloads\recording.mp4"

# Custom title
python skaldfetch.py "https://..." --title "My Custom Title"

# Spotify with manual show name (if auto-detection fails)
python skaldfetch.py "https://open.spotify.com/episode/..." --show "Satisfying Software"
```

## Spotify resolution

Spotify uses DRM, so yt-dlp can't download from it directly.
SkjaldFetch works around this automatically:

1. Gets the episode title from Spotify's oEmbed API (no auth needed)
2. Extracts the show name from the Spotify page
3. Searches the iTunes API for the podcast's public RSS feed
4. Finds the episode in the RSS feed and downloads the MP3 directly

This works for ~95% of podcasts. Shows that are Spotify-exclusive (no public RSS) will fail with a clear error.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | *(required)* | AssemblyAI API key |
| `OUTPUT_DIR` | `transcriptions/` next to script | Where to save `.md` files |
| `BERTHA_INBOX` | *(blank = disabled)* | Path to Bertha inbox folder |
