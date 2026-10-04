"""Narration audio: a neural voice via edge-tts (Microsoft's online TTS), falling back to macOS `say`.

Usage: tts.py <narration.json> <work dir>. Writes <work>/audio/<id>.wav (48 kHz, soft fades so
clips never click) and <work>/durations.json. `text` is what subtitles show; `say` (optional)
or the PRONOUNCE map is what the voice reads.
"""
import asyncio, json, os, subprocess, sys
from pathlib import Path

VOICE = os.environ.get("TTS_VOICE", "en-IN-NeerjaExpressiveNeural")
RATE = os.environ.get("TTS_RATE", "+16%")
PRONOUNCE = {"SerpApi": "Serp API", "Groq": "Grok", "LeadLoop": "Lead Loop", "SQLite": "S Q Lite"}


def spoken(item: dict) -> str:
    text = item.get("say") or item["text"]
    for written, said in PRONOUNCE.items():
        text = text.replace(written, said)
    return text


def duration(path) -> float:
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]))


async def neural(text: str, out: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(out))


def main(narration: str, work: str) -> None:
    audio = Path(work) / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    durs, engine = {}, "edge-tts"
    for item in json.load(open(narration)):
        raw = audio / f"{item['id']}.raw"
        words = len(spoken(item).split())
        try:
            # The service sometimes returns a clip cut off mid-sentence: no natural voice reads
            # faster than ~5 words a second, so anything shorter than that is retried.
            for attempt in range(4):
                asyncio.run(neural(spoken(item), raw))
                if duration(raw) >= words / 5:
                    break
                print(f"   {item['id']}: clip was truncated ({duration(raw):.1f}s for {words} words), retrying")
            else:
                raise RuntimeError("clip kept coming back truncated")
        except Exception as e:  # offline or service down: keep going with the built-in voice
            engine = f"macOS say for some clips (edge-tts failed: {type(e).__name__})"
            raw = audio / f"{item['id']}.aiff"
            subprocess.run(["say", "-v", "Aman", "-r", "175", "-o", str(raw), spoken(item)], check=True)
        d = duration(raw)
        wav = audio / f"{item['id']}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-ar", "48000", "-ac", "1",
                        "-af", f"afade=t=in:d=0.03,afade=t=out:st={max(d - 0.08, 0):.3f}:d=0.08", str(wav)], check=True)
        raw.unlink()
        durs[item["id"]] = round(duration(wav), 2)
    json.dump(durs, open(Path(work) / "durations.json", "w"), indent=1)
    print(f"   {sum(durs.values()):.0f}s of speech · {engine} · {VOICE} {RATE}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
