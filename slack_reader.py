"""Read a Slack channel and maintain a shared coordination document.

    python slack_reader.py --channel notes-live --watch
    python slack_reader.py --channel notes-live --once
    python slack_reader.py --post status "indexing the arm calibration code"

Two audiences share one channel:
  - notetaker posts live meeting notes (human-readable)
  - agents post structured updates (machine-readable)

An agent update is a ```agent fenced JSON block, which Slack renders as a code
block for humans and this reader parses for machines:

    ```agent
    {"agent": "impl-alpha", "type": "claim", "task": "camera calibration",
     "note": "starting now", "project": "arm-skills"}
    ```

Types: status | claim | done | blocked | question | note | decision

Everything is merged into COORDINATION.md — the file other agents read before
they start work, so two agents do not claim the same task.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slack_client import Slack, SlackError, load_config  # noqa: E402

from paths import coordination_doc, reader_state                               # noqa: E402

DEFAULT_DOC = coordination_doc()
STATE = reader_state()

# Both fences must start a line. Without that anchor the non-greedy match ends
# at the first ``` anywhere — including one inside a JSON string value, which
# happens the moment an agent's own note mentions the protocol.
BLOCK_RE = re.compile(r"^```agent[ \t]*\n(.*?)\n^```", re.S | re.M | re.I)
# Matches the poster's parent message, e.g. "📝 *Standup* — live notes · update 3".
NOTES_RE = re.compile(r"\b(live|final) notes\b", re.I)
VALID_TYPES = {"status", "claim", "done", "blocked", "question", "note", "decision"}


# ----------------------------------------------------------------------

def parse_agent_blocks(text: str) -> list[dict]:
    """Extract ```agent JSON blocks. Malformed blocks are reported, not dropped."""
    out = []
    for raw in BLOCK_RE.findall(text or ""):
        try:
            obj = json.loads(raw.strip())
        except json.JSONDecodeError as e:
            out.append({"type": "note", "agent": "unknown", "_parse_error": str(e),
                        "note": raw.strip()[:200]})
            continue
        if isinstance(obj, dict):
            obj.setdefault("type", "note")
            obj.setdefault("agent", "unknown")
            if obj["type"] not in VALID_TYPES:
                obj["type"] = "note"
            out.append(obj)
    return out


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(s: dict):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def ts_human(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "?"


# ----------------------------------------------------------------------

def build_doc(channel_name: str, channel_id: str, events: list[dict],
              notes_excerpt: str) -> str:
    """Render COORDINATION.md from the accumulated event log."""
    claims: dict[str, dict] = {}
    for e in events:
        if e["type"] in ("claim", "done", "blocked"):
            key = (e.get("task") or "").strip().lower()
            if not key:
                continue
            prev = claims.get(key, {})
            # `done` and `blocked` supersede an earlier claim on the same task
            if prev.get("type") == "done" and e["type"] != "done":
                continue
            claims[key] = e

    open_items = [c for c in claims.values() if c["type"] == "claim"]
    blocked = [c for c in claims.values() if c["type"] == "blocked"]
    done = [c for c in claims.values() if c["type"] == "done"]
    questions = [e for e in events if e["type"] == "question"][-10:]
    decisions = [e for e in events if e["type"] == "decision"][-15:]

    agents = {}
    for e in events:
        a = e.get("agent", "unknown")
        if a not in agents or e["_ts"] > agents[a]["_ts"]:
            agents[a] = e

    L = []
    L.append("# Project Coordination")
    L.append("")
    L.append(f"_Auto-generated from Slack `#{channel_name}` ({channel_id}) — "
             f"last sync {datetime.now():%Y-%m-%d %H:%M}._")
    L.append("_Do not hand-edit: this file is rewritten on every sync. "
             "To change it, post to the channel (see SLACK_AGENTS.md)._")
    L.append("")

    L.append("## Active agents")
    if agents:
        L.append("")
        L.append("| Agent | Project | Last seen | Last activity |")
        L.append("|---|---|---|---|")
        for name, e in sorted(agents.items()):
            last = (e.get("note") or e.get("task") or e["type"])[:60]
            L.append(f"| `{name}` | {e.get('project', '—')} | "
                     f"{ts_human(e['_ts'])} | {e['type']}: {last} |")
    else:
        L.append("\n_None yet._")
    L.append("")

    L.append("## Claimed / in progress")
    if open_items:
        L.append("")
        for e in sorted(open_items, key=lambda x: x["_ts"], reverse=True):
            note = f" — {e['note']}" if e.get("note") else ""
            L.append(f"- **{e.get('task')}** — `{e.get('agent')}` "
                     f"({ts_human(e['_ts'])}){note}")
    else:
        L.append("\n_Nothing claimed. Claim before you start work._")
    L.append("")

    if blocked:
        L.append("## Blocked")
        L.append("")
        for e in sorted(blocked, key=lambda x: x["_ts"], reverse=True):
            L.append(f"- ⚠️ **{e.get('task')}** — `{e.get('agent')}`: "
                     f"{e.get('note', 'no detail given')}")
        L.append("")

    if decisions:
        L.append("## Decisions")
        L.append("")
        for e in decisions:
            L.append(f"- {e.get('note') or e.get('task')} "
                     f"— `{e.get('agent')}`, {ts_human(e['_ts'])}")
        L.append("")

    if questions:
        L.append("## Open questions")
        L.append("")
        for e in questions:
            L.append(f"- {e.get('note') or e.get('task')} "
                     f"— asked by `{e.get('agent')}`, {ts_human(e['_ts'])}")
        L.append("")

    if done:
        L.append("## Completed")
        L.append("")
        for e in sorted(done, key=lambda x: x["_ts"], reverse=True)[:20]:
            L.append(f"- ✅ **{e.get('task')}** — `{e.get('agent')}` "
                     f"({ts_human(e['_ts'])})")
        L.append("")

    if notes_excerpt:
        L.append("## Latest meeting notes")
        L.append("")
        L.append("_Posted by notetaker from live audio. Speech-to-text is lossy; "
                 "treat `(?)` markers as unverified._")
        L.append("")
        L.append(notes_excerpt.strip())
        L.append("")

    L.append("---")
    L.append(f"_{len(events)} agent messages ingested. "
             "Protocol: `SLACK_AGENTS.md`_")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------

def sync(slack: Slack, channel: dict, doc_path: Path, log) -> int:
    state = load_state()
    key = channel["id"]
    chan_state = state.setdefault(key, {})
    events = chan_state.get("events", [])
    seen = {e["_ts"] for e in events}

    # Scan a recent window rather than only messages newer than last sync: the
    # notetaker's parent message is edited in place, so its ts never advances
    # and an incremental cursor would skip it forever. Agent events are deduped
    # by uid, so re-scanning is free.
    msgs = slack.history(key, limit=300)
    new = 0
    notes_excerpt = chan_state.get("notes_excerpt", "")

    for m in msgs:
        ts = m.get("ts")
        text = m.get("text", "")

        # Capture the most recent notetaker post as the notes excerpt.
        # The poster keeps one parent message carrying only the summary and
        # threads each full update beneath it, so the detail is in the replies —
        # which conversations.history does not return. Fetch them.
        if NOTES_RE.search(text):
            body = text.split("\n", 2)[-1]
            if m.get("reply_count"):
                try:
                    reps = slack.replies(key, m["ts"])
                    full = [r.get("text", "") for r in reps
                            if r.get("ts") != m.get("ts")]
                    if full:
                        body = full[-1]
                except SlackError:
                    pass                    # fall back to the summary
            notes_excerpt = body[:4000]

        for obj in parse_agent_blocks(text):
            uid = f"{ts}:{obj.get('agent')}:{obj.get('type')}:{obj.get('task', '')}"
            if uid in seen:
                continue
            seen.add(uid)
            obj["_ts"] = ts
            obj["_uid"] = uid
            obj["_user"] = m.get("user", "")
            events.append(obj)
            new += 1

    events.sort(key=lambda e: float(e.get("_ts", 0)))
    chan_state["events"] = events[-1000:]
    chan_state["notes_excerpt"] = notes_excerpt
    if msgs:
        chan_state["oldest_next"] = msgs[-1]["ts"]
    save_state(state)

    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(
        build_doc(channel["name"], channel["id"], events, notes_excerpt),
        encoding="utf-8")

    log(f"synced: {len(msgs)} messages scanned, {new} new agent events, "
        f"{len(events)} total -> {doc_path}")
    return new


def post_update(slack: Slack, channel_id: str, agent: str, kind: str,
                task: str | None, note: str | None, project: str | None):
    payload = {"agent": agent, "type": kind}
    if task:
        payload["task"] = task
    if note:
        payload["note"] = note
    if project:
        payload["project"] = project
    icon = {"claim": "🔒", "done": "✅", "blocked": "⚠️", "question": "❓",
            "decision": "📌", "status": "⚙️", "note": "💬"}.get(kind, "💬")
    text = (f"{icon} *{agent}* — {kind}" + (f": {task}" if task else "") + "\n"
            + "```agent\n" + json.dumps(payload, indent=2) + "\n```")
    slack.post(channel_id, text)


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default=(cfg.get("slack") or {}).get("channel_name",
                                                                     "notes-live"))
    ap.add_argument("--channel-id", default=(cfg.get("slack") or {}).get("channel_id")
                    or None,
                    help="read this existing channel id (skips lookup/creation; "
                         "needed if the token lacks channels:write)")
    ap.add_argument("--doc", default=str((cfg.get("coordination") or {}).get(
        "doc_path", DEFAULT_DOC)))
    ap.add_argument("--watch", action="store_true", help="poll continuously")
    ap.add_argument("--once", action="store_true", help="sync once and exit")
    ap.add_argument("--interval", type=int, default=(cfg.get("coordination") or {}).get(
        "poll_seconds", 60))
    ap.add_argument("--post", nargs=2, metavar=("TYPE", "NOTE"),
                    help="post an agent update, e.g. --post claim 'camera calib'")
    ap.add_argument("--agent", default="reader", help="agent name for --post")
    ap.add_argument("--task", help="task name for --post")
    ap.add_argument("--project", help="project name for --post")
    args = ap.parse_args()

    def log(m):
        print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)

    try:
        slack = Slack()
    except SlackError as e:
        log(f"cannot reach Slack: {e}")
        return 1

    try:
        if args.channel_id:
            channel = slack.channel_info(args.channel_id)
        else:
            channel = slack.ensure_channel(args.channel)
        log(f"channel #{channel['name']} ({channel['id']})")
    except SlackError as e:
        hint = ("  (this token cannot create channels — pass --channel-id "
                "for one that already exists)" if e.error == "missing_scope" else "")
        log(f"channel error: {e}{hint}")
        return 1

    if args.post:
        kind, note = args.post
        post_update(slack, channel["id"], args.agent, kind,
                    args.task, note, args.project)
        log(f"posted {kind} as {args.agent}")
        return 0

    doc = Path(args.doc)
    if args.watch:
        log(f"watching #{channel['name']} every {args.interval}s -> {doc}")
        while True:
            try:
                sync(slack, channel, doc, log)
            except SlackError as e:
                log(f"sync failed: {e}")
            time.sleep(args.interval)
    else:
        sync(slack, channel, doc, log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
