"""Audio capture: microphone + Windows system audio (WASAPI loopback).

Two sources are captured independently so the transcript can label who spoke:
  - "mic"    -> you (your microphone)
  - "system" -> everyone else (whatever is playing out of your speakers)

Each source runs its own thread, resamples to 16 kHz mono, and emits
speech-bounded segments onto a shared queue for the transcriber.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from queue import Queue

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

TARGET_SR = 16000  # what whisper wants


@dataclass
class Segment:
    source: str          # "mic" | "system"
    audio: np.ndarray    # float32 mono @ 16 kHz
    t_start: float       # seconds since session start
    t_end: float
    peak_rms: float


# --------------------------------------------------------------------------
# device selection
# --------------------------------------------------------------------------

def list_devices(pa: pyaudio.PyAudio) -> dict:
    """Return {'inputs': [...], 'loopbacks': [...], 'default_output': str}."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    out = {"inputs": [], "loopbacks": [], "default_output": None}

    try:
        dflt = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        out["default_output"] = dflt["name"]
    except Exception:
        pass

    for i in range(wasapi["deviceCount"]):
        d = pa.get_device_info_by_host_api_device_index(wasapi["index"], i)
        if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice", False):
            out["inputs"].append((d["index"], d["name"], int(d["defaultSampleRate"])))

    for lb in pa.get_loopback_device_info_generator():
        out["loopbacks"].append((lb["index"], lb["name"], int(lb["defaultSampleRate"])))

    return out


def pick_mic(pa: pyaudio.PyAudio, want: int | None = None) -> dict | None:
    """Default WASAPI input device, or an explicit index."""
    if want is not None:
        return pa.get_device_info_by_index(want)
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    idx = wasapi.get("defaultInputDevice", -1)
    if idx is None or idx < 0:
        return None
    try:
        return pa.get_device_info_by_index(idx)
    except Exception:
        return None


def pick_loopback(pa: pyaudio.PyAudio, want: int | None = None) -> dict | None:
    """Loopback device matching the current default output (i.e. what you hear)."""
    if want is not None:
        return pa.get_device_info_by_index(want)
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    except Exception:
        default_out = ""

    loopbacks = list(pa.get_loopback_device_info_generator())
    if not loopbacks:
        return None
    for lb in loopbacks:
        if default_out and default_out in lb["name"]:
            return lb
    return loopbacks[0]


# --------------------------------------------------------------------------
# speech segmentation
# --------------------------------------------------------------------------

class Segmenter:
    """Cuts a continuous stream into speech-bounded segments.

    Fixed-size chunks slice words in half; this waits for a natural pause so
    whisper sees whole phrases. Falls back to a hard cut at max_s.
    """

    FRAME_MS = 20

    def __init__(self, sr=TARGET_SR, min_s=5.0, max_s=25.0, gap_s=0.7,
                 floor_mult=3.5, abs_floor=0.004):
        self.sr = sr
        self.frame = int(sr * self.FRAME_MS / 1000)
        self.min_n = int(min_s * sr)
        self.max_n = int(max_s * sr)
        self.gap_frames = int(gap_s * 1000 / self.FRAME_MS)
        self.floor_mult = floor_mult
        self.abs_floor = abs_floor

        self.buf = np.zeros(0, dtype=np.float32)
        self.tail = np.zeros(0, dtype=np.float32)   # partial frame carry-over
        self.noise = 0.01
        self.silence_run = 0
        self.speech_frames = 0
        self.peak = 0.0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Add audio; return zero or more completed segments."""
        data = np.concatenate([self.tail, samples])
        n_frames = len(data) // self.frame
        self.tail = data[n_frames * self.frame:]
        done = []

        for i in range(n_frames):
            f = data[i * self.frame:(i + 1) * self.frame]
            rms = float(np.sqrt(np.mean(f * f)) + 1e-9)

            # Track the noise floor: drop fast toward quiet, rise slowly.
            if rms < self.noise:
                self.noise = 0.90 * self.noise + 0.10 * rms
            else:
                self.noise = 0.999 * self.noise + 0.001 * rms

            is_speech = rms > max(self.noise * self.floor_mult, self.abs_floor)
            if is_speech:
                self.speech_frames += 1
                self.silence_run = 0
                self.peak = max(self.peak, rms)
            else:
                self.silence_run += 1

            self.buf = np.concatenate([self.buf, f])

            long_enough = len(self.buf) >= self.min_n
            paused = self.silence_run >= self.gap_frames
            if (long_enough and paused) or len(self.buf) >= self.max_n:
                if self.speech_frames >= 8:      # ~160 ms of actual speech
                    done.append(self.buf.copy())
                self._reset()

        return done

    def flush(self) -> np.ndarray | None:
        """Emit whatever is buffered (called at stop)."""
        out = None
        if len(self.buf) >= self.sr * 1.0 and self.speech_frames >= 8:
            out = self.buf.copy()
        self._reset()
        return out

    def _reset(self):
        self.buf = np.zeros(0, dtype=np.float32)
        self.silence_run = 0
        self.speech_frames = 0
        self.peak = 0.0


# --------------------------------------------------------------------------
# capture thread
# --------------------------------------------------------------------------

class SourceCapture(threading.Thread):
    """Reads one device, resamples to 16 kHz mono, emits Segments."""

    def __init__(self, pa: pyaudio.PyAudio, dev: dict, source: str,
                 out_q: Queue, t0: float, stop_evt: threading.Event,
                 gain: float = 1.0, on_error=None):
        super().__init__(daemon=True, name=f"capture-{source}")
        self.pa = pa
        self.dev = dev
        self.source = source
        self.q = out_q
        self.t0 = t0
        self.stop_evt = stop_evt
        self.gain = gain
        self.on_error = on_error

        self.sr_in = int(dev["defaultSampleRate"])
        self.channels = int(dev["maxInputChannels"])
        self.frames_per_read = 2048
        self.seg = Segmenter()
        self.level = 0.0          # live meter, 0..1
        self.total_seconds = 0.0
        self.error: str | None = None

    def run(self):
        stream = None
        try:
            stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sr_in,
                input=True,
                input_device_index=self.dev["index"],
                frames_per_buffer=self.frames_per_read,
            )
            cursor = 0.0
            while not self.stop_evt.is_set():
                raw = stream.read(self.frames_per_read, exception_on_overflow=False)
                mono = self._to_mono16k(raw)
                if mono is None or len(mono) == 0:
                    continue

                dur = len(mono) / TARGET_SR
                self.total_seconds += dur
                self.level = float(np.sqrt(np.mean(mono * mono)))

                for chunk in self.seg.feed(mono):
                    seg_dur = len(chunk) / TARGET_SR
                    self.q.put(Segment(self.source, chunk, cursor, cursor + seg_dur,
                                       float(np.sqrt(np.mean(chunk * chunk)))))
                    cursor += seg_dur
                    continue
                cursor = max(cursor, self.total_seconds - len(self.seg.buf) / TARGET_SR)

            leftover = self.seg.flush()
            if leftover is not None:
                self.q.put(Segment(self.source, leftover, cursor,
                                   cursor + len(leftover) / TARGET_SR,
                                   float(np.sqrt(np.mean(leftover * leftover)))))
        except Exception as e:                                  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            if self.on_error:
                self.on_error(self.source, self.error)
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass

    def _to_mono16k(self, raw: bytes) -> np.ndarray | None:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if self.channels > 1:
            usable = (len(a) // self.channels) * self.channels
            a = a[:usable].reshape(-1, self.channels).mean(axis=1)
        if self.gain != 1.0:
            a = np.clip(a * self.gain, -1.0, 1.0)
        if self.sr_in != TARGET_SR:
            g = np.gcd(self.sr_in, TARGET_SR)
            a = resample_poly(a, TARGET_SR // g, self.sr_in // g).astype(np.float32)
        return a
