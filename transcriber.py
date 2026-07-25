"""Local Whisper transcription with hallucination filtering.

Runs on CPU. One model instance, one worker thread, segments processed in
arrival order. Whisper invents text when fed silence or music, so anything
low-confidence or matching the well-known filler phrases is dropped.
"""

from __future__ import annotations

import re
import threading
import time
from queue import Empty, Queue

import numpy as np

# Phrases whisper emits when there is nothing to transcribe (YouTube-caption
# training residue). Matched against the whole segment, lowercased.
JUNK_EXACT = {
    "you", "thank you.", "thank you", "thanks for watching!", "thanks for watching.",
    "thanks for watching", "please subscribe.", "subscribe!", "bye.", "bye bye.",
    "okay.", "ok.", ".", "..", "...", "!", "?", "so", "so.", "yeah.", "hmm.",
    "[music]", "[blank_audio]", "[silence]", "(upbeat music)", "music",
    "the end.", "the end", "oh.", "mm-hmm.", "uh.", "um.",
}
JUNK_PATTERNS = [
    re.compile(r"subtitles?\s+by", re.I),
    re.compile(r"amara\.org", re.I),
    re.compile(r"subscribe to (my|our|the) channel", re.I),
    re.compile(r"^\s*[\W_]*\s*$"),                 # punctuation / symbols only
    re.compile(r"^(♪|\[.*\])+$"),
    re.compile(r"www\.\S+\.(com|org|net)", re.I),
]


def collapse_repeats(text: str) -> str:
    """Whisper loops on unclear audio, repeating a phrase many times.

    The whole-segment junk check misses this because the segment also contains
    real speech. Collapse runs of near-identical sentences down to one.
    """
    parts = re.split(r"(?<=[.!?])\s+", text)
    out, prev, run = [], None, 0
    for p in parts:
        key = re.sub(r"[^a-z0-9 ]", "", p.lower()).strip()
        if key and key == prev:
            run += 1
            if run >= 1:            # keep the first, drop every repeat after it
                continue
        else:
            prev, run = key, 0
        out.append(p)

    joined = " ".join(out)
    # Also catch loops that never got sentence punctuation, e.g. a phrase of
    # 3+ words repeated back-to-back many times inside one run-on sentence.
    joined = re.sub(r"\b((?:\w+[ ,]+){2,8}?)(?:\1){2,}", r"\1", joined, flags=re.I)
    return re.sub(r"\s+", " ", joined).strip()


def is_junk(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if t.lower() in JUNK_EXACT:
        return True
    for p in JUNK_PATTERNS:
        if p.search(t):
            return True
    # A single short word repeated over and over is a hallucination loop.
    words = t.lower().split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


class Transcriber(threading.Thread):
    """Pulls Segments off a queue, writes transcript lines via on_line()."""

    def __init__(self, in_q: Queue, on_line, stop_evt: threading.Event,
                 model_name: str = "base", language: str | None = "en",
                 log=print):
        super().__init__(daemon=True, name="transcriber")
        self.q = in_q
        self.on_line = on_line
        self.stop_evt = stop_evt
        self.model_name = model_name
        self.language = language
        self.log = log

        self.model = None
        self.done_count = 0
        self.dropped_count = 0
        self.audio_seconds = 0.0
        self.compute_seconds = 0.0
        self.ready = threading.Event()

    def load(self):
        import torch
        import whisper

        torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))
        t0 = time.time()
        self.model = whisper.load_model(self.model_name)
        self.log(f"  whisper '{self.model_name}' loaded in {time.time() - t0:.1f}s")
        self.ready.set()

    def run(self):
        if self.model is None:
            self.load()

        while True:
            try:
                seg = self.q.get(timeout=0.4)
            except Empty:
                if self.stop_evt.is_set() and self.q.empty():
                    break
                continue

            try:
                self._handle(seg)
            except Exception as e:                              # noqa: BLE001
                self.log(f"  [transcribe error] {type(e).__name__}: {e}")
            finally:
                self.q.task_done()

    def _handle(self, seg):
        audio = seg.audio.astype(np.float32)
        dur = len(audio) / 16000.0
        if dur < 0.8:
            self.dropped_count += 1
            return

        t0 = time.time()
        result = self.model.transcribe(
            audio,
            language=self.language,
            fp16=False,
            condition_on_previous_text=False,   # stops runaway repetition loops
            temperature=0.0,
            no_speech_threshold=0.6,
            logprob_threshold=-1.0,
        )
        elapsed = time.time() - t0
        self.audio_seconds += dur
        self.compute_seconds += elapsed

        kept = []
        for s in result.get("segments", []):
            text = s.get("text", "").strip()
            if not text or is_junk(text):
                continue
            if s.get("no_speech_prob", 0.0) > 0.65 and s.get("avg_logprob", 0.0) < -0.6:
                continue
            if s.get("avg_logprob", 0.0) < -1.1:
                continue
            kept.append(text)

        if not kept:
            self.dropped_count += 1
            return

        text = " ".join(kept).strip()
        text = re.sub(r"\s+", " ", text)
        text = collapse_repeats(text)
        if is_junk(text):
            self.dropped_count += 1
            return

        self.done_count += 1
        self.on_line(seg.source, seg.t_start, text)

    @property
    def realtime_factor(self) -> float:
        """Seconds of audio processed per second of compute. >1 keeps up live."""
        if self.compute_seconds <= 0:
            return 0.0
        return self.audio_seconds / self.compute_seconds
