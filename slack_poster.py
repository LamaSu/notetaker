"""Post live meeting notes to a Slack channel on an interval.

    python slack_poster.py --session <dir> --channel notes-live --every 300

Behaviour:
  - Creates the channel if it does not exist, then joins it.
  - Waits for the first notes.md to appear, then posts it immediately.
  - Re-posts every --every seconds, but only when the notes actually changed
    (an identical re-post every 5 minutes is noise, not signal).
  - If no Slack token is configured yet it keeps waiting and posts the moment
    one appears, so it can be started before credentials are in place.
  - Posts a final message when the session ends (recorder gone + notes stable).

State lives in <session>/.slack-state.json so restarts resume cleanly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slack_client import Slack, SlackError, chunk, get_token, to_mrkdwn  # noqa: E402

NOTES_ROOT = Path(r"C:\Users\globa\notes")
HEADER = "📝 *Live notes — {title}*  ·  update {n}  ·  {clock}"


def latest_session() -> Path | None:
    if not NOTES_ROOT.exists():
        return None
    dirs = [d for d in NOTES_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def load_state(session: Path) -> dict:
    p = session / ".slack-state.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(session: Path, state: dict):
    (session / ".slack-state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8")


def transcript_lines(session: Path) -> int:
    t = session / "transcript.md"
    if not t.exists():
        return 0
    return sum(1 for l in t.read_text(encoding="utf-8", errors="replace").splitlines()
               if l.startswith("["))


def session_title(session: Path) -> str:
    t = session / "transcript.md"
    if t.exists():
        for line in t.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return session.name


def recorder_running(session: Path) -> bool:
    """The recorder writes STOP-file / final notes; treat a stale transcript as done."""
    t = session / "transcript.md"
    if not t.exists():
        return True
    return (time.time() - t.stat().st_mtime) < 900  # 15 min of silence = over


def wait_for_slack(channel_name: str, channel_id: str | None,
                   purpose: str, log) -> tuple[Slack, dict]:
    """Block until a token exists and the channel is resolved."""
    warned = False
    while True:
        if get_token():
            try:
                s = Slack()
                me = s.whoami()
                log(f"authenticated: {me.get('team')} as {me.get('user')}")

                if channel_id:
                    ch = s.channel_info(channel_id)
                    log(f"using existing channel #{ch['name']} ({ch['id']})")
                    return s, ch

                try:
                    ch = s.ensure_channel(channel_name, purpose)
                    log(f"channel #{ch['name']} ({ch['id']}) "
                        f"{'created' if ch.get('_created') else 'already existed'}")
                    return s, ch
                except SlackError as e:
                    if e.error == "missing_scope":
                        raise SlackError("ensure_channel", "missing_scope",
                                         "token cannot create channels; pass "
                                         "--channel-id for an existing one")
                    raise
            except SlackError as e:
                log(f"slack not usable yet: {e}")
                if e.error == "missing_scope":
                    raise
        elif not warned:
            warned = True
            log("waiting for a Slack token "
                "(SLACK_BOT_TOKEN / SLACK_MCP_XOXP_TOKEN / config.json)...")
        time.sleep(15)


def extract_summary(notes: str) -> str:
    """Pull the **Summary** paragraph out of the notes for the parent message."""
    m = re.search(r"\*\*Summary\*\*\s*[—-]?\s*(.+?)(?:\n\n|\n#)", notes, re.S)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip()
    body = [l for l in notes.splitlines() if l.strip() and not l.startswith("#")]
    return (body[0][:400] if body else "(no summary yet)")


def build_parent(session: Path, n: int, final: bool = False) -> str:
    """The single top-level message, rewritten in place on every update.

    Kept short on purpose: this sits in the channel timeline, and the detail
    lives in the thread so a 5-minute cadence does not bury other traffic.
    """
    notes = (session / "notes.md").read_text(encoding="utf-8", errors="replace")
    title = session_title(session)
    icon = "✅" if final else "📝"
    state = "final" if final else "live"
    head = (f"{icon} *{title}* — {state} notes  ·  "
            f"update {n}  ·  {datetime.now():%H:%M}")
    return (f"{head}\n\n{to_mrkdwn(extract_summary(notes))}\n\n"
            f"_Full notes in thread ↓ · {transcript_lines(session)} transcript "
            f"lines · `{session.name}`_")


def build_reply(session: Path, n: int, final: bool = False) -> list[str]:
    """The full notes, posted as a threaded reply."""
    notes = (session / "notes.md").read_text(encoding="utf-8", errors="replace").strip()
    head = (f"*{'Final' if final else 'Update'} {n}* · {datetime.now():%H:%M}")
    return chunk(head + "\n\n" + to_mrkdwn(notes))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session dir (default: most recent)")
    ap.add_argument("--channel", default="notes-live", help="channel name to create/use")
    ap.add_argument("--channel-id", help="post to this existing channel id "
                                         "(skips creation; needed if the token "
                                         "lacks channels:write)")
    ap.add_argument("--every", type=int, default=300, help="seconds between posts")
    ap.add_argument("--once", action="store_true", help="post once and exit")
    args = ap.parse_args()

    session = Path(args.session) if args.session else latest_session()
    if not session or not session.exists():
        print("no session found; start a recording first")
        return 1

    def log(msg):
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

    log(f"session: {session}")
    state = load_state(session)

    purpose = (f"Live meeting notes posted automatically by notetaker. "
               f"Session {session.name}. Agent integration: see SLACK_AGENTS.md")
    slack, channel = wait_for_slack(args.channel, args.channel_id, purpose, log)

    state.update({"channel_id": channel["id"], "channel_name": channel["name"]})
    save_state(session, state)

    notes_path = session / "notes.md"

    while True:
        # Re-read state every cycle rather than trusting an in-memory copy.
        # A manual `--once` run (or a second daemon) writes the same file, and
        # a stale copy would duplicate an update number and clobber their work.
        state = load_state(session)
        state.setdefault("channel_id", channel["id"])
        state.setdefault("channel_name", channel["name"])
        last_hash = state.get("last_hash")
        n = state.get("post_count", 0)
        posted_final = state.get("posted_final", False)

        alive = recorder_running(session)

        if notes_path.exists():
            content = notes_path.read_text(encoding="utf-8", errors="replace")
            h = hashlib.sha256(content.encode("utf-8")).hexdigest()
            final = (not alive) and not posted_final

            if h != last_hash or final:
                n += 1
                try:
                    parent_ts = state.get("parent_ts")

                    if parent_ts:
                        # Keep the channel timeline to one message: rewrite it.
                        slack.update(channel["id"], parent_ts,
                                     build_parent(session, n, final=final))
                    else:
                        r = slack.post(channel["id"],
                                       build_parent(session, n, final=final))
                        parent_ts = r["ts"]
                        state["parent_ts"] = parent_ts

                    for part in build_reply(session, n, final=final):
                        slack.post(channel["id"], part, thread_ts=parent_ts)

                    last_hash = h
                    if final:
                        posted_final = True
                    state.update({"last_hash": last_hash, "post_count": n,
                                  "posted_final": posted_final,
                                  "last_post": datetime.now().isoformat()})
                    save_state(session, state)
                    log(f"posted update {n} to #{channel['name']} "
                        f"(thread {parent_ts})" + (" [final]" if final else ""))
                except SlackError as e:
                    n -= 1
                    log(f"post failed: {e}")
            else:
                log("notes unchanged — nothing to post")
        else:
            log("waiting for first notes.md ...")

        if args.once or posted_final:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
