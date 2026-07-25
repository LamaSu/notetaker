"""notetaker — local meeting notes: mic + system audio -> Whisper -> Haiku notes.

    note start ["Title"]     record until Ctrl+C, then write organized notes
    note list                list past sessions
    note organize <session>  re-run the Haiku pass on an existing transcript
    note devices             show audio devices
    note test [seconds]      short capture + transcribe smoke test

Everything runs locally except the note-organizing pass.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Queue

import pyaudiowpatch as pyaudio

sys.path.insert(0, str(Path(__file__).resolve().parent))

from audio_capture import SourceCapture, list_devices, pick_loopback, pick_mic  # noqa: E402
from transcriber import Transcriber                                            # noqa: E402

from paths import notes_root                                                   # noqa: E402

NOTES_ROOT = notes_root()
LABEL = {"mic": "You", "system": "Others"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    s = re.sub(r"[\s_]+", "-", s)
    return (s[:limit].strip("-")) or "session"


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def meter(level: float, width: int = 10) -> str:
    filled = min(width, int(level * width * 8))
    return "#" * filled + "." * (width - filled)


# --------------------------------------------------------------------------
# recording session
# --------------------------------------------------------------------------

class Session:
    def __init__(self, title: str, model: str, use_mic: bool, use_system: bool,
                 organize_every: int, mic_index=None, sys_index=None,
                 mic_gain: float = 1.0, backend: str = "auto"):
        self.title = title
        self.model = model
        self.use_mic = use_mic
        self.use_system = use_system
        self.organize_every = organize_every
        self.mic_index = mic_index
        self.sys_index = sys_index
        self.mic_gain = mic_gain
        self.backend = backend

        started = datetime.now()
        self.dir = NOTES_ROOT / f"{started:%Y-%m-%d}-{started:%H%M}-{slugify(title)}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = self.dir / "transcript.md"
        self.notes_path = self.dir / "notes.md"

        self.t0 = time.time()
        self.started = started
        self.lines = 0
        self.lock = threading.Lock()
        self.recent: list[str] = []
        self.errors: list[str] = []

    def write_header(self, sources: list[str]):
        with self.transcript_path.open("w", encoding="utf-8") as f:
            f.write(f"# {self.title}\n\n")
            f.write(f"- Started: {self.started:%Y-%m-%d %H:%M:%S}\n")
            f.write(f"- Sources: {', '.join(sources)}\n")
            f.write(f"- Model: whisper `{self.model}` (local)\n\n")
            f.write("---\n\n")

    def add_line(self, source: str, t_start: float, text: str):
        stamp = hms(time.time() - self.t0)
        line = f"[{stamp}] {LABEL.get(source, source)}: {text}"
        with self.lock:
            with self.transcript_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.lines += 1
            self.recent.append(line)
            del self.recent[:-6]

    def note_error(self, source: str, msg: str):
        self.errors.append(f"{source}: {msg}")


def record(args) -> int:
    from organizer import organize_session

    NOTES_ROOT.mkdir(parents=True, exist_ok=True)
    title = args.title or f"Session {datetime.now():%b %d %H:%M}"
    sess = Session(
        title=title, model=args.model,
        use_mic=not args.no_mic, use_system=not args.no_system,
        organize_every=args.organize_every,
        mic_index=args.mic_device, sys_index=args.system_device,
        mic_gain=args.mic_gain, backend=args.backend,
    )

    pa = pyaudio.PyAudio()
    stop_evt = threading.Event()
    seg_q: Queue = Queue()
    captures = []
    sources = []

    try:
        if sess.use_mic:
            dev = pick_mic(pa, sess.mic_index)
            if dev is None:
                print("  ! no microphone found — continuing without mic")
            else:
                captures.append(SourceCapture(pa, dev, "mic", seg_q, sess.t0, stop_evt,
                                              gain=sess.mic_gain, on_error=sess.note_error))
                sources.append(f"mic \"{dev['name'][:38]}\" (You)")

        if sess.use_system:
            dev = pick_loopback(pa, sess.sys_index)
            if dev is None:
                print("  ! no WASAPI loopback device — continuing without system audio")
            else:
                captures.append(SourceCapture(pa, dev, "system", seg_q, sess.t0, stop_evt,
                                              on_error=sess.note_error))
                sources.append(f"system \"{dev['name'][:38]}\" (Others)")

        if not captures:
            print("No audio sources available. Run `note devices` to inspect.")
            return 1

        sess.write_header(sources)

        print(f"\n  {title}")
        print(f"  {sess.dir}\n")
        for s in sources:
            print(f"  + {s}")

        print(f"\n  loading whisper '{args.model}' (first run downloads the model)...")
        tr = Transcriber(seg_q, sess.add_line, stop_evt,
                         model_name=args.model, language=args.language,
                         log=lambda m: print(m))
        tr.load()
        tr.start()
        for c in captures:
            c.start()

        stop_file = sess.dir / "STOP"
        print("\n  Recording. Press Ctrl+C to stop and write notes.")
        print(f"  (if detached, stop with:  ni '{stop_file}')\n")

        last_org = time.time()
        last_render = 0.0
        try:
            while True:
                time.sleep(0.25)
                now = time.time()

                if stop_file.exists():
                    print("\n\n  STOP file seen — stopping...")
                    break

                if now - last_render >= 0.5:
                    last_render = now
                    bits = [f"  {hms(now - sess.t0)}"]
                    for c in captures:
                        bits.append(f"{LABEL[c.source][:6]} [{meter(c.level)}]")
                    bits.append(f"lines {sess.lines}")
                    if seg_q.qsize():
                        bits.append(f"queue {seg_q.qsize()}")
                    sys.stdout.write("\r" + "  ".join(bits) + "   ")
                    sys.stdout.flush()

                for c in captures:
                    if c.error and c.source not in "".join(sess.errors):
                        print(f"\n  ! {c.source} capture stopped: {c.error}")

                if sess.organize_every and now - last_org >= sess.organize_every:
                    last_org = now
                    if sess.lines:
                        threading.Thread(
                            target=_organize_quiet, args=(sess, args.backend),
                            daemon=True,
                        ).start()
        except KeyboardInterrupt:
            print("\n\n  stopping...")

        stop_evt.set()
        for c in captures:
            c.join(timeout=5)
        tr.join(timeout=180)

    finally:
        pa.terminate()

    dur = time.time() - sess.t0
    print(f"  captured {hms(dur)}, {sess.lines} transcript lines")
    if tr.realtime_factor:
        print(f"  whisper ran at {tr.realtime_factor:.1f}x realtime")

    if not sess.lines:
        print("\n  No speech captured — nothing to organize.")
        print(f"  Transcript: {sess.transcript_path}")
        return 0

    print("  organizing with haiku...")
    try:
        notes_path = organize_session(sess.dir, backend=args.backend)
        print(f"\n  Notes:      {notes_path}")
        print(f"  Transcript: {sess.transcript_path}\n")
    except Exception as e:                                       # noqa: BLE001
        print(f"\n  ! organizing failed: {e}")
        print(f"  Transcript is safe at: {sess.transcript_path}")
        print(f"  Retry with: note organize \"{sess.dir}\"\n")
        return 1
    return 0


def _organize_quiet(sess: Session, backend: str):
    """Periodic mid-session pass; failures here are not worth interrupting for."""
    from organizer import organize_session
    try:
        organize_session(sess.dir, backend=backend)
    except Exception:                                            # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# other commands
# --------------------------------------------------------------------------

def cmd_devices(_args) -> int:
    pa = pyaudio.PyAudio()
    try:
        info = list_devices(pa)
        print(f"\n  default output: {info['default_output']}\n")
        print("  INPUT (microphones)")
        for idx, name, sr in info["inputs"]:
            print(f"    {idx:>3}  {name[:52]:<52} {sr} Hz")
        print("\n  LOOPBACK (system audio — what you hear)")
        for idx, name, sr in info["loopbacks"]:
            print(f"    {idx:>3}  {name[:52]:<52} {sr} Hz")
        print("\n  Use --mic-device N / --system-device N to override.\n")
    finally:
        pa.terminate()
    return 0


def cmd_list(_args) -> int:
    if not NOTES_ROOT.exists():
        print(f"\n  no sessions yet ({NOTES_ROOT})\n")
        return 0
    rows = sorted((d for d in NOTES_ROOT.iterdir() if d.is_dir()), reverse=True)
    if not rows:
        print(f"\n  no sessions yet ({NOTES_ROOT})\n")
        return 0
    print()
    for d in rows:
        t = d / "transcript.md"
        n = d / "notes.md"
        lines = 0
        if t.exists():
            lines = sum(1 for l in t.read_text(encoding="utf-8").splitlines()
                        if l.startswith("["))
        print(f"  {d.name:<52} {lines:>4} lines  notes:{'yes' if n.exists() else ' no'}")
    print(f"\n  {NOTES_ROOT}\n")
    return 0


def cmd_organize(args) -> int:
    from organizer import organize_session
    target = Path(args.session)
    if not target.is_absolute():
        target = NOTES_ROOT / args.session
    if not target.exists():
        print(f"  no such session: {target}")
        return 1
    print("  organizing with haiku...")
    path = organize_session(target, backend=args.backend)
    print(f"  Notes: {path}")
    return 0


def cmd_test(args) -> int:
    seconds = args.seconds
    pa = pyaudio.PyAudio()
    stop_evt = threading.Event()
    seg_q: Queue = Queue()
    got: list[str] = []

    try:
        caps = []
        mic = pick_mic(pa, None)
        lb = pick_loopback(pa, None)
        if mic:
            caps.append(SourceCapture(pa, mic, "mic", seg_q, time.time(), stop_evt))
            print(f"  mic:      {mic['name'][:50]}")
        if lb:
            caps.append(SourceCapture(pa, lb, "system", seg_q, time.time(), stop_evt))
            print(f"  loopback: {lb['name'][:50]}")
        if not caps:
            print("  no devices")
            return 1

        print(f"\n  loading whisper '{args.model}'...")
        tr = Transcriber(seg_q, lambda s, t, x: got.append(f"{LABEL[s]}: {x}"),
                         stop_evt, model_name=args.model, language=args.language)
        tr.load()
        tr.start()
        for c in caps:
            c.start()

        print(f"  listening {seconds}s — say something, or play audio...\n")
        for i in range(seconds):
            time.sleep(1)
            bits = [f"  {i + 1:>2}/{seconds}"]
            for c in caps:
                bits.append(f"{LABEL[c.source][:6]} [{meter(c.level)}]")
            sys.stdout.write("\r" + "  ".join(bits) + "   ")
            sys.stdout.flush()

        stop_evt.set()
        for c in caps:
            c.join(timeout=5)
        tr.join(timeout=120)
    finally:
        pa.terminate()

    print("\n")
    for line in got:
        print(f"  {line}")
    if not got:
        print("  (nothing transcribed — check levels above were moving)")
    if tr.realtime_factor:
        print(f"\n  whisper: {tr.realtime_factor:.1f}x realtime, "
              f"{tr.done_count} kept, {tr.dropped_count} dropped as silence/noise")
    for c in caps:
        if c.error:
            print(f"  ! {c.source}: {c.error}")
    print()
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(prog="note", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    common = dict(model="base", language="en", backend="auto")

    s = sub.add_parser("start", help="record a session")
    s.add_argument("title", nargs="?", help="session title")
    s.add_argument("--model", default="base",
                   choices=["tiny", "base", "small", "medium"],
                   help="whisper model (default: base)")
    s.add_argument("--language", default="en", help="spoken language, or 'auto'")
    s.add_argument("--no-mic", action="store_true", help="skip microphone")
    s.add_argument("--no-system", action="store_true", help="skip system audio")
    s.add_argument("--mic-device", type=int, help="mic device index")
    s.add_argument("--system-device", type=int, help="loopback device index")
    s.add_argument("--mic-gain", type=float, default=1.0, help="mic gain multiplier")
    s.add_argument("--organize-every", type=int, default=300,
                   help="seconds between mid-session note passes (0 = only at end)")
    s.add_argument("--backend", default="auto", choices=["auto", "cli", "sdk"],
                   help="haiku backend (default: auto)")
    s.set_defaults(func=record)

    s = sub.add_parser("list", help="list sessions")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("organize", help="re-run notes for a session")
    s.add_argument("session", help="session dir name or full path")
    s.add_argument("--backend", default="auto", choices=["auto", "cli", "sdk"])
    s.set_defaults(func=cmd_organize)

    s = sub.add_parser("devices", help="list audio devices")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("test", help="short capture + transcribe check")
    s.add_argument("seconds", nargs="?", type=int, default=12)
    s.add_argument("--model", default="base")
    s.add_argument("--language", default="en")
    s.set_defaults(func=cmd_test)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    for k, v in common.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
