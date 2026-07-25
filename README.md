# notetaker

Local meeting notes for Windows. Captures your microphone **and** your system
audio, transcribes both on-device with Whisper, and has Claude organize the
result into structured notes every few minutes.

Optionally posts those notes to Slack and maintains a shared coordination
document so several agents working on one project can see what the humans in
the room actually decided.

No cloud transcription. No account to sign into. Audio never leaves the
machine — only the finished transcript is sent for organizing, and that runs
through your existing Claude subscription with no API key.

---

## Why

Meeting-notes tools want an account, a calendar integration, and a seat in
every call. This one wants a microphone.

The system-audio half is the part most tools get wrong on Windows: capturing
*what the other people said* means a WASAPI loopback capture of the render
stream, not a second microphone. notetaker records both streams separately, so
the transcript can tell you apart from everyone else.

---

## What it produces

```
notes/2026-07-25-1318-standup/
  transcript.md    # timestamped, speaker-labelled, appended live
  notes.md         # summary / key points / decisions / action items / open questions
```

`transcript.md` while recording:

```
[00:04:12] You: the first skill we want to build is picking an object off the open shelf
[00:04:31] Others: and how do we handle orientation when it's not square to the camera
```

---

## Requirements

- Windows 10/11 (WASAPI loopback is Windows-specific)
- Python 3.11+
- `pip install PyAudioWPatch openai-whisper scipy numpy`
- FFmpeg on `PATH` (Whisper needs it)
- Claude Code CLI for the organizing pass, **or** an Anthropic API key

The first run downloads a Whisper model (~140 MB for `base`).

---

## Usage

```bash
python notetaker.py start "Standup"     # record until Ctrl+C
python notetaker.py list                # past sessions
python notetaker.py organize <session>  # re-run the note pass on a transcript
python notetaker.py devices             # list microphones and loopback devices
python notetaker.py test 12             # 12-second capture + transcribe check
```

Run `test` first. It shows live level meters for both sources and prints what
it heard, which tells you in twelve seconds whether your devices are right.

Useful flags:

| Flag | Effect |
|---|---|
| `--model tiny\|base\|small\|medium` | Whisper size. `base` is the default. |
| `--no-mic` / `--no-system` | Record only one side |
| `--mic-device N` / `--system-device N` | Override device selection |
| `--organize-every N` | Seconds between note passes (default 300, `0` = only at the end) |
| `--language auto` | Non-English or mixed audio |

Pick the model to fit the machine: `base` runs about 2.7× realtime on a 2-core
i7-7660U, so it keeps up live with margin. `small` is noticeably more accurate
but roughly 3× slower — comfortable on a modern multi-core CPU, too slow on an
old ultrabook. Check the `Nx realtime` number `test` prints; below ~1.5× the
queue will fall behind on a long meeting.

---

## Accuracy, honestly

This is speech-to-text on room audio, so it is lossy — more so with far-field
microphones, crosstalk, and jargon. Two mitigations are built in:

- **Loop collapsing.** Whisper repeats itself on unclear audio ("we can use the
  handkerchief" ×8). Runs of near-identical sentences are collapsed to one.
- **Hallucination filtering.** Segments that are silence artifacts ("Thanks for
  watching!", "Subtitles by…") or low-confidence are dropped rather than
  written.

The organizing prompt is instructed to mark uncertain terms `(?)` rather than
guess silently. **Do not treat a number, name, or commitment from these notes
as authoritative** — confirm it before acting on it.

---

## Slack integration (optional)

Posts notes to a channel on an interval, and reads that channel back into a
shared coordination document.

```bash
# post notes every 5 minutes
python slack_poster.py --channel-id C0123456789 --every 300

# keep COORDINATION.md current from the channel
python slack_reader.py --watch --interval 60
```

To avoid burying a busy channel, the poster keeps **one** top-level message per
session and rewrites it in place with the latest summary; each full update goes
into that message's thread.

Token resolution order: `SLACK_BOT_TOKEN`, `SLACK_MCP_XOXP_TOKEN`,
`SLACK_USER_TOKEN`, `config.json`, then the Windows User environment. Scopes
needed: `chat:write`, `channels:read`, `channels:history`, `channels:join`.
Channel creation additionally needs `channels:manage` (bot) or `channels:write`
(user token) — without it, pass `--channel-id` for a channel that already
exists.

### Agent coordination

Agents post structured updates as fenced `agent` blocks — a code block to
humans, parseable to machines:

````
```agent
{"agent": "impl-alpha", "type": "claim", "task": "camera calibration",
 "note": "starting now", "project": "arm-skills"}
```
````

Types: `claim`, `done`, `blocked`, `status`, `question`, `decision`, `note`.

`slack_reader.py` folds these into `COORDINATION.md` — active agents, what is
claimed, what is blocked, decisions already made, open questions, plus the
latest meeting notes. Agents read it before starting work so two of them do not
claim the same task.

Full protocol: [`SLACK_AGENTS.md`](SLACK_AGENTS.md).

---

## How it works

```
mic ─────────┐                          ┌─ transcript.md (live)
             ├─ WASAPI ─ resample 16k ─ VAD segmentation ─ Whisper ─┤
system audio ┘  loopback                                            └─ Haiku ─ notes.md
```

Audio is cut on natural pauses rather than fixed intervals, so Whisper sees
whole phrases instead of half-words. Both sources share one model instance and
are transcribed in arrival order.

| File | Role |
|---|---|
| `notetaker.py` | CLI, session lifecycle, live display |
| `audio_capture.py` | WASAPI capture, resampling, pause-based segmentation |
| `transcriber.py` | Whisper worker, hallucination and loop filtering |
| `organizer.py` | Transcript → structured notes |
| `slack_client.py` | Slack Web API wrapper (stdlib only) |
| `slack_poster.py` | Posts notes on an interval |
| `slack_reader.py` | Channel → `COORDINATION.md` |

---

## Privacy

Recording captures whatever is on your microphone and speakers, including other
people. Recording others may require their consent depending on where you are —
that call is yours to make.

Transcripts and notes stay in `notes/`, which is gitignored. The only thing
leaving the machine is the transcript text sent for organizing, plus whatever
you choose to post to Slack.

---

## License

MIT
