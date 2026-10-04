"""Edits the take: builds the video from captured frames, cuts idle waits, lays each narration
clip at its mark, writes subtitles.

Audio is built so it can't glitch: clips never overlap (a minimum gap is enforced), each has soft
fades (added in tts.py), and the mix is loudness-normalised and limited once at the end.
"""
import json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).parent
DIR = Path(os.environ.get("WORK", HERE / "work")).resolve()
OUT = Path(sys.argv[1])
marks = json.loads((DIR / "marks.json").read_text())
dur = json.loads((DIR / "durations.json").read_text())
text = {s["id"]: s["text"] for s in json.loads((HERE / "narration.json").read_text())}
GAP = 0.35  # minimum silence between two narration clips

# --- 1. frames -> constant-rate video on the recording's clock ---------------------------------
frames = marks["frames"]
offset = frames[0]["t"]
total = marks["total"] - offset
M = [{"id": m["id"], "t": m["t"] - offset} for m in marks["marks"]]
shows = [{"t": x["t"] - offset, "d": x["d"]} for x in marks.get("shows", [])]
lines = []
for i, fr in enumerate(frames):
    end = frames[i + 1]["t"] if i + 1 < len(frames) else marks["total"]
    lines += [f"file '{DIR / 'frames' / fr['f']}'", f"duration {max(end - fr['t'], 0.001):.4f}"]
lines.append(f"file '{DIR / 'frames' / frames[-1]['f']}'")
(DIR / "frames.txt").write_text("\n".join(lines))
raw = DIR / "raw.mp4"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(DIR / "frames.txt"),
                "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(raw)], check=True)

# --- 2. what to keep: narrated stretches and marked "show" moments; shorten the waits between ---
spans = sorted([(m["t"], m["t"] + dur[m["id"]] + 0.4) for m in M] + [(x["t"], x["t"] + x["d"]) for x in shows])
keep = [(0.0, spans[0][0])]
for i, (s, e) in enumerate(spans):
    keep.append((s, e))
    if i + 1 < len(spans):
        nxt = spans[i + 1][0]
        if nxt - e > 2.2:
            keep += [(e, e + 0.4), (nxt - 1.0, nxt)]  # a beat after, and the result arriving
        elif nxt > e:
            keep.append((e, nxt))
keep.append((spans[-1][1], min(total, spans[-1][1] + 1.0)))
merged = []
for a, b in sorted(keep):
    if merged and a <= merged[-1][1] + 0.05:
        merged[-1] = (merged[-1][0], max(merged[-1][1], b))
    elif b - a > 0.05:
        merged.append((a, b))


def new_time(t):
    acc = 0.0
    for a, b in merged:
        if t >= b:
            acc += b - a
        elif t >= a:
            return acc + (t - a)
    return acc


final_len = sum(b - a for a, b in merged)

# --- 3. narration timeline: never overlapping ---------------------------------------------------
starts, prev_end = [], 0.0
for m in M:
    t = max(new_time(m["t"]), prev_end + GAP if starts else 0.0)
    starts.append((m["id"], t))
    prev_end = t + dur[m["id"]]
assert prev_end <= final_len + 0.5, "narration runs past the end of the video"

v = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{k}];" for k, (a, b) in enumerate(merged))
v += "".join(f"[v{k}]" for k in range(len(merged))) + f"concat=n={len(merged)}:v=1:a=0,fps=30,format=yuv420p[vout];"
a = "".join(f"[{i + 1}:a]aresample=48000,adelay={int(t * 1000)}:all=1[a{i}];" for i, (_, t) in enumerate(starts))
a += ("".join(f"[a{i}]" for i in range(len(starts)))
      + f"amix=inputs={len(starts)}:normalize=0:dropout_transition=0,apad,atrim=0:{final_len:.3f},"
        "loudnorm=I=-16:TP=-1.5:LRA=11,alimiter=limit=0.9,aresample=48000[aout]")
cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(raw)]
for sid, _ in starts:
    cmd += ["-i", str(DIR / "audio" / f"{sid}.wav")]
cmd += ["-filter_complex", v + a, "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart", str(OUT)]
subprocess.run(cmd, check=True)


def ts(t):
    h, r = divmod(t, 3600); m_, s = divmod(r, 60)
    return f"{int(h):02}:{int(m_):02}:{int(s):02},{int((s % 1) * 1000):03}"


srt = [f"{n}\n{ts(t)} --> {ts(t + dur[sid])}\n{text[sid]}\n" for n, (sid, t) in enumerate(starts, 1)]
OUT.with_suffix(".srt").write_text("\n".join(srt))
gaps = [starts[i + 1][1] - (starts[i][1] + dur[starts[i][0]]) for i in range(len(starts) - 1)]
print(f"raw {total:.1f}s -> final {final_len:.1f}s · {len(merged)} kept pieces · smallest gap between lines {min(gaps):.2f}s · {OUT}")
print("timeline:", ", ".join(f"{sid}@{t:.1f}s" for sid, t in starts))
if final_len > 178:
    print(f"WARNING: {final_len:.0f}s is over the hackathon's 3-minute limit")
