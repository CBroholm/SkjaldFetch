#!/usr/bin/env python3
"""
SkjaldFetch — download + transcribe a podcast or video URL.

Downloads audio silently (no playback), transcribes with AssemblyAI
speaker diarization, and saves the result as a Markdown file.

Spotify URLs are resolved automatically: the episode title is looked up
via Spotify's oEmbed API, the show's public RSS feed is found via the
iTunes Search API, and the unprotected MP3 is downloaded from there.

Usage:
    python skaldfetch.py <url-or-local-file> [--title "Custom title"] [--show "Podcast name"]

Examples:
    python skaldfetch.py "https://open.spotify.com/episode/..."
    python skaldfetch.py "https://www.youtube.com/watch?v=..."
    python skaldfetch.py "https://feeds.example.com/episode.mp3"
    python skaldfetch.py "C:\\Downloads\\recording.mp4" --title "Team standup"
    python skaldfetch.py "https://open.spotify.com/episode/..." --show "Satisfying Software"

Requirements:
    pip install yt-dlp assemblyai python-dotenv requests

Configuration (.env):
    ASSEMBLYAI_API_KEY=your_key_here
    OUTPUT_DIR=C:\\path\\to\\output\\folder
    BERTHA_INBOX=C:\\path\\to\\bertha\\inbox
"""

import os, sys, re, argparse, tempfile, threading, time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import assemblyai as aai

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env", override=True)
aai.settings.api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")

_raw_output = os.environ.get("OUTPUT_DIR", "")
OUTPUT_DIR  = Path(_raw_output) if _raw_output else Path(__file__).parent / "transcriptions"

_raw_bertha  = os.environ.get("BERTHA_INBOX", "")
BERTHA_INBOX = Path(_raw_bertha) if _raw_bertha else None

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_duration(seconds: int) -> str:
    if not seconds:
        return "ukendt varighed"
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}t {m}m {s}s" if h else f"{m}m {s}s"


def slug(text: str, max_len: int = 45) -> str:
    return re.sub(r"[^\w\-]", "_", text)[:max_len].strip("_")


class Spinner:
    """Simple CLI spinner for long-running steps."""
    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self, label: str):
        self.label   = label
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        sys.stdout.write(f"\r{' ' * (len(self.label) + 6)}\r")
        sys.stdout.flush()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            sys.stdout.write(f"\r  {self.FRAMES[i % len(self.FRAMES)]}  {self.label}")
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1


# ── Spotify resolver ──────────────────────────────────────────────────────────

def resolve_spotify(url: str, show_hint: str = "") -> tuple[str, str, str]:
    """
    Given a Spotify episode URL, find the same episode on a public RSS feed.

    Returns (mp3_url, episode_title, show_name).
    Raises ValueError with a helpful message if resolution fails.
    """
    import requests
    from xml.etree import ElementTree as ET

    # Step 1 — episode title via Spotify oEmbed (no auth, reliable)
    try:
        oembed = requests.get(
            "https://open.spotify.com/oembed",
            params={"url": url}, timeout=10).json()
        episode_title = oembed.get("title", "").strip()
    except Exception as e:
        raise ValueError(f"Kunne ikke hente Spotify-metadata: {e}")

    if not episode_title:
        raise ValueError("Spotify oEmbed returnerede ingen titel.")

    print(f"  Episode:  {episode_title}")

    # Step 2 — show name from Spotify page meta tags
    show_name = show_hint.strip()
    if not show_name:
        try:
            page = requests.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text

            # Pattern A: <meta name="twitter:title" content="Show: Episode">
            m = re.search(r'<meta name="twitter:title" content="([^"]+)"', page)
            if m:
                parts = m.group(1).split(":", 1)
                if len(parts) == 2:
                    show_name = parts[0].strip()

            # Pattern B: <meta property="og:description" content="... Show Name on Spotify">
            if not show_name:
                m2 = re.search(
                    r'<meta property="og:description" content="[^"]*?([A-Z][^"]+?) on Spotify',
                    page)
                if m2:
                    show_name = m2.group(1).strip()

            # Pattern C: JSON-LD or other embedded data with "show" key
            if not show_name:
                m3 = re.search(r'"show"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', page)
                if m3:
                    show_name = m3.group(1).strip()

        except Exception:
            pass  # fall through to error below

    if not show_name:
        # Fallback: search iTunes for the episode title — returns collectionName (show name)
        try:
            ep_search = requests.get(
                "https://itunes.apple.com/search",
                params={"term": episode_title, "entity": "podcastEpisode", "limit": 5},
                timeout=10).json()
            for r in ep_search.get("results", []):
                candidate = r.get("collectionName", "").strip()
                if candidate:
                    show_name = candidate
                    break
        except Exception:
            pass

    if not show_name:
        raise ValueError(
            "Kunne ikke finde podcast-navn automatisk.\n"
            "  Brug:  --show 'Podcast Name'  for at angive det manuelt.")

    print(f"  Show:     {show_name}")
    print(f"  Søger i iTunes og offentlige RSS-feeds…")

    # Step 3 — find the RSS feed via iTunes Search API (free, no auth)
    try:
        search = requests.get(
            "https://itunes.apple.com/search",
            params={"term": show_name, "entity": "podcast", "limit": 5},
            timeout=10).json()
    except Exception as e:
        raise ValueError(f"iTunes-søgning fejlede: {e}")

    results = search.get("results", [])
    if not results:
        raise ValueError(
            f"Ingen podcast fundet med navn '{show_name}' i iTunes.\n"
            f"  Prøv et kortere/anderledes navn med --show.")

    # Step 4 — search each RSS feed for the episode
    ep_words = set(w for w in episode_title.lower().split() if len(w) > 3)
    threshold = max(3, len(ep_words) // 2)

    for result in results:
        feed_url = result.get("feedUrl")
        if not feed_url:
            continue
        try:
            rss_resp = requests.get(feed_url, timeout=20)
            rss_resp.raise_for_status()
            root = ET.fromstring(rss_resp.text)
        except Exception:
            continue

        for item in root.findall(".//item"):
            item_title = item.findtext("title", "")
            item_words = set(w for w in item_title.lower().split() if len(w) > 3)
            if len(ep_words & item_words) >= threshold:
                enc = item.find("enclosure")
                if enc is not None and enc.get("url"):
                    mp3_url = enc.get("url")
                    print(f"  Fundet i RSS: {result.get('collectionName', show_name)}")
                    return mp3_url, episode_title, show_name

    raise ValueError(
        f"Fandt '{show_name}' på iTunes, men episoden\n"
        f"  '{episode_title}'\n"
        f"  er ikke i det offentlige RSS-feed.\n"
        f"  Episoden er muligvis Spotify-eksklusiv.")


# ── Download ──────────────────────────────────────────────────────────────────

def _progress_hook(d):
    if d["status"] == "downloading":
        pct = d.get("_percent_str", "").strip()
        spd = d.get("_speed_str",   "").strip()
        sys.stdout.write(f"\r  ↓  Download: {pct:>6}  {spd:>12}  ")
        sys.stdout.flush()
    elif d["status"] == "finished":
        sys.stdout.write("\r  ✓  Download færdig" + " " * 20 + "\n")
        sys.stdout.flush()


def download_audio(url: str, dest_dir: Path) -> tuple[str, str, int]:
    """
    Download best available audio with yt-dlp.
    Returns (audio_filepath, title, duration_seconds).
    Works without FFmpeg by preferring native audio formats.
    """
    try:
        import yt_dlp
    except ImportError:
        print("\n  yt-dlp ikke installeret. Kør:  pip install yt-dlp\n")
        sys.exit(1)

    # First pass: fetch metadata only
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info     = ydl.extract_info(url, download=False)
        title    = info.get("title") or Path(url).stem
        duration = info.get("duration") or 0

    print(f"  Titel:    {title}")
    print(f"  Varighed: {fmt_duration(duration)}")
    print()

    dl_opts = {
        "format":         "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[ext=aac]/bestaudio",
        "outtmpl":        str(dest_dir / "audio.%(ext)s"),
        "quiet":          True,
        "no_warnings":    True,
        "progress_hooks": [_progress_hook],
    }
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([url])

    candidates = [f for f in dest_dir.iterdir() if f.is_file()]
    if not candidates:
        raise FileNotFoundError("yt-dlp producerede ingen lydfil.")
    audio_path = sorted(candidates, key=lambda f: f.stat().st_mtime)[-1]

    return str(audio_path), title, duration


def download_direct(url: str, dest_dir: Path) -> str:
    """Download a direct audio URL (e.g. from RSS enclosure) with requests."""
    import requests
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    ext  = url.split("?")[0].rsplit(".", 1)[-1] or "mp3"
    dest = dest_dir / f"audio.{ext}"
    total  = int(resp.headers.get("content-length", 0))
    downloaded = 0
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    sys.stdout.write(f"\r  ↓  Download: {pct:5.1f}%  ")
                    sys.stdout.flush()
    sys.stdout.write("\r  ✓  Download færdig" + " " * 20 + "\n")
    sys.stdout.flush()
    return str(dest)


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe(audio_path: str) -> list:
    """Upload to AssemblyAI and return list of utterances."""
    config = aai.TranscriptionConfig(
        speaker_labels=True,
        speech_models=[aai.SpeechModel.universal],
    )
    transcript = aai.Transcriber().transcribe(audio_path, config=config)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI fejl: {transcript.error}")
    return transcript.utterances or []


# ── Markdown ──────────────────────────────────────────────────────────────────

def build_markdown(source: str, title: str, duration: int, utterances: list) -> str:
    is_local    = Path(source).exists() if len(source) < 300 else False
    source_line = f"**Fil:** {Path(source).name}" if is_local else f"**Kilde:** {source}"

    lines = [
        f"# 📻 {title}",
        "",
        source_line,
        f"**Dato:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Varighed:** {fmt_duration(duration)}",
        f"**Ytringer:** {len(utterances)}",
        "**Type:** Podcast/video-transskription (SkjaldFetch)",
        "",
        "---",
        "",
    ]

    current_speaker = None
    for utt in utterances:
        if utt.speaker != current_speaker:
            if current_speaker is not None:
                lines.append("")
            lines.append(f"**Speaker {utt.speaker}:**")
            current_speaker = utt.speaker
        lines.append(utt.text)

    lines.append("")
    return "\n".join(lines)


# ── Save ──────────────────────────────────────────────────────────────────────

def save_files(content: str, title: str) -> tuple[Path, Path | None]:
    ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{slug(title)}_{ts}.md"

    out_path = OUTPUT_DIR / filename
    out_path.write_text(content, encoding="utf-8")

    bertha_path = None
    if BERTHA_INBOX is not None:
        try:
            BERTHA_INBOX.mkdir(parents=True, exist_ok=True)
            bertha_path = BERTHA_INBOX / filename
            bertha_preamble = (
                "> **Til Bertha** — Dette er en podcast/video-transskription fra SkjaldFetch.\n"
                "> Lav et kort resumé med de vigtigste pointer og læg det i vores næste daglige opsamling.\n\n"
                "---\n\n"
            )
            bertha_path.write_text(bertha_preamble + content, encoding="utf-8")
        except Exception as e:
            print(f"  (Bertha-skrivning fejlede: {e})")

    return out_path, bertha_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="SkjaldFetch — transkribér podcast/video til markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url",     help="URL til video/podcast eller sti til lokal lydfil")
    parser.add_argument("--title", help="Overskrid auto-detekteret titel")
    parser.add_argument("--show",  help="Podcast-navn til Spotify-søgning (bruges hvis auto-detektion fejler)")
    args = parser.parse_args()

    url      = args.url
    is_local = Path(url).exists()

    print()
    print("  SkjaldFetch")
    print("  " + "─" * 48)
    print()

    if not aai.settings.api_key:
        print("  FEJL: ASSEMBLYAI_API_KEY mangler i .env\n")
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # ── Acquire audio ──────────────────────────────────────────────────────
        if is_local:
            audio_path = url
            title      = args.title or Path(url).stem
            duration   = 0
            print(f"  Fil:      {Path(url).name}")
            print()

        elif "open.spotify.com" in url:
            print(f"  Spotify URL — søger i offentligt RSS-feed…")
            print()
            try:
                mp3_url, ep_title, show_name = resolve_spotify(url, show_hint=args.show or "")
            except ValueError as e:
                print(f"\n  FEJL: {e}\n")
                sys.exit(1)

            title    = args.title or f"{show_name} — {ep_title}"
            duration = 0
            print()
            try:
                audio_path = download_direct(mp3_url, tmp)
            except Exception as e:
                print(f"\n  FEJL ved download af RSS-lyd: {e}\n")
                sys.exit(1)

        else:
            print(f"  URL:      {url[:72]}{'...' if len(url) > 72 else ''}")
            print()
            try:
                audio_path, title, duration = download_audio(url, tmp)
            except Exception as e:
                print(f"  FEJL ved download: {e}")
                print("  Tip: opdatér yt-dlp med:  pip install -U yt-dlp\n")
                sys.exit(1)

        if args.title:
            title = args.title

        # ── Transcribe ────────────────────────────────────────────────────────
        print("  Upload + transskription (vent venligst)…")
        try:
            with Spinner("Transskriberer med AssemblyAI"):
                utterances = transcribe(audio_path)
        except Exception as e:
            print(f"\n  FEJL ved transskription: {e}\n")
            sys.exit(1)

        print(f"  ✓  {len(utterances)} ytringer transskriberet")
        print()

        # ── Build + save ──────────────────────────────────────────────────────
        content          = build_markdown(url, title, duration, utterances)
        out_path, bertha = save_files(content, title)

        print(f"  Arkiv  →  {out_path}")
        if bertha:
            print(f"  Bertha →  {bertha}")
            print()
            print("  Bertha finder den i indbakken ✓")
        else:
            print(f"  Transskription gemt i arkivet.")
            if BERTHA_INBOX:
                print(f"  (Bertha-mappe ikke fundet: {BERTHA_INBOX})")

        print()


if __name__ == "__main__":
    main()
