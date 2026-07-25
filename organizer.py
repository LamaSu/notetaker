"""Turn a raw transcript into organized notes using Haiku.

Two backends, tried in order:
  1. `claude -p --model haiku`  — keyless, uses the Claude Code subscription
  2. anthropic SDK              — needs ANTHROPIC_API_KEY or an `ant auth login`
                                  profile; faster (~2s vs ~30s) when available

The prompt is deliberately strict about only using what's in the transcript —
speech-to-text is lossy, and notes that invent detail are worse than no notes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

MODEL_SDK = "claude-haiku-4-5"

SYSTEM = """You organize raw meeting transcripts into clean notes.

The transcript comes from automatic speech-to-text, so it contains errors:
misheard words, missing punctuation, dropped fragments. Work with what is
there. Never invent content that is not in the transcript.

Rules:
- Only state things actually said. No filler, no assumed context.
- Speaker labels: "You" is the person recording (their microphone).
  "Others" is everyone else (captured from their speakers).
- If a name, number, or term is clearly garbled, write your best reading
  followed by (?) — e.g. "Q3 revenue was 4.2 million (?)".
- If a section has nothing in it, omit that section entirely. Do not write
  "None" or "N/A".
- Output GitHub-flavoured markdown. No preamble, no sign-off, no code fences
  around the whole document.
"""

TEMPLATE = """Organize the transcript below into notes with this structure:

# <a short descriptive title>

**Summary** — 2-4 sentences on what this was about and what came out of it.

## Key points
- The substantive content. Group related points; drop small talk.

## Decisions
- Things that were actually decided. Omit this section if nothing was.

## Action items
- [ ] Task — **owner** (if stated) — due date (if stated)

## Open questions
- Things raised but not resolved.

## Details worth keeping
- Specific numbers, names, dates, links, tools mentioned.

---
TRANSCRIPT:

{transcript}
"""


def _clean(text: str) -> str:
    """Strip the harness timestamp preamble and any wrapping code fence."""
    lines = text.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("🕐")):
        lines.pop(0)
    out = "\n".join(lines).strip()
    if out.startswith("```"):
        parts = out.split("```")
        if len(parts) >= 3:
            body = parts[1]
            body = re.sub(r"^(markdown|md)\n", "", body)
            out = body.strip()
    return out


def _via_cli(prompt: str, timeout: int = 300) -> str:
    """Keyless path: pipe the transcript to `claude -p --model haiku`."""
    cmd = [
        "claude", "-p", "--model", "haiku",
        "--append-system-prompt",
        "Output only the requested markdown document. No preamble, no commentary.",
        SYSTEM,
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr[:400]}")
    return _clean(proc.stdout)


def _via_sdk(prompt: str) -> str:
    """API path: faster, needs a key or an `ant auth login` profile."""
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL_SDK,
        max_tokens=8000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined to organize this transcript")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return _clean(text)


def organize(transcript: str, backend: str = "auto") -> str:
    """transcript -> organized markdown notes."""
    transcript = transcript.strip()
    if not transcript:
        return "_(no speech captured yet)_"

    prompt = TEMPLATE.format(transcript=transcript)

    order = []
    if backend == "auto":
        order = ["sdk", "cli"] if os.environ.get("ANTHROPIC_API_KEY") else ["cli", "sdk"]
    else:
        order = [backend]

    errors = []
    for b in order:
        try:
            return _via_sdk(prompt) if b == "sdk" else _via_cli(prompt)
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"{b}: {type(e).__name__}: {e}")
    raise RuntimeError("all organizer backends failed -> " + " | ".join(errors))


def organize_session(session_dir: Path, backend: str = "auto") -> Path:
    """Read transcript.md from a session dir, write notes.md, return its path."""
    session_dir = Path(session_dir)
    transcript_path = session_dir / "transcript.md"
    if not transcript_path.exists():
        raise FileNotFoundError(f"no transcript at {transcript_path}")

    raw = transcript_path.read_text(encoding="utf-8")
    # Drop the header block; keep only the timestamped speech lines.
    body = "\n".join(l for l in raw.splitlines() if re.match(r"^\[\d\d:\d\d:\d\d\]", l))

    notes = organize(body, backend=backend)
    notes_path = session_dir / "notes.md"
    notes_path.write_text(notes + "\n", encoding="utf-8")
    return notes_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python organizer.py <session-dir>")
        raise SystemExit(2)
    print(organize_session(Path(sys.argv[1])))
