"""Minimal Slack Web API client — stdlib only, no dependencies.

Credentials are read in this order:
  1. SLACK_BOT_TOKEN environment variable
  2. "slack.token" in C:\\Users\\globa\\notetaker\\config.json

Needs a bot token (xoxb-...) with these scopes:
  chat:write, channels:manage, channels:read, channels:history, channels:join
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://slack.com/api/"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class SlackError(RuntimeError):
    """A Slack API call returned ok:false."""

    def __init__(self, method: str, error: str, detail=None):
        self.method = method
        self.error = error
        self.detail = detail
        super().__init__(f"{method}: {error}" + (f" ({detail})" if detail else ""))


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


TOKEN_VARS = ("SLACK_BOT_TOKEN", "SLACK_MCP_XOXP_TOKEN", "SLACK_USER_TOKEN")


def _user_env(name: str) -> str | None:
    """Read a Windows User-scope environment variable.

    A detached background process does not necessarily inherit variables that
    were set after its parent's shell started, so fall back to the registry.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return winreg.QueryValueEx(k, name)[0]
    except (ImportError, FileNotFoundError, OSError):
        return None


def get_token() -> str | None:
    for var in TOKEN_VARS:
        tok = os.environ.get(var)
        if tok and tok.strip():
            return tok.strip()

    tok = (load_config().get("slack") or {}).get("token")
    if tok and tok.strip() and not tok.startswith("xoxb-PASTE"):
        return tok.strip()

    for var in TOKEN_VARS:
        tok = _user_env(var)
        if tok and tok.strip():
            return tok.strip()
    return None


class Slack:
    def __init__(self, token: str | None = None):
        self.token = token or get_token()
        if not self.token:
            raise SlackError("init", "no_token",
                             "set SLACK_BOT_TOKEN or slack.token in config.json")

    def call(self, method: str, params: dict | None = None,
             post: bool = True, retries: int = 3) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last = None

        for attempt in range(retries):
            try:
                if post:
                    body = json.dumps(params).encode("utf-8")
                    req = urllib.request.Request(
                        API + method, data=body,
                        headers={"Authorization": f"Bearer {self.token}",
                                 "Content-Type": "application/json; charset=utf-8"})
                else:
                    url = API + method
                    if params:
                        url += "?" + urllib.parse.urlencode(params)
                    req = urllib.request.Request(
                        url, headers={"Authorization": f"Bearer {self.token}"})

                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                if data.get("ok"):
                    return data

                err = data.get("error", "unknown")
                # Rate limited: honour Retry-After and try again.
                if err == "ratelimited" and attempt < retries - 1:
                    time.sleep(float(data.get("retry_after", 5)))
                    continue
                raise SlackError(method, err, data.get("needed") or data.get("response_metadata"))

            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < retries - 1:
                    time.sleep(float(e.headers.get("Retry-After", 5)))
                    continue
                last = SlackError(method, f"http_{e.code}", e.reason)
            except urllib.error.URLError as e:
                last = SlackError(method, "network", str(e.reason))
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            if last:
                raise last
        raise last or SlackError(method, "unknown")

    # -- identity -------------------------------------------------------

    def whoami(self) -> dict:
        return self.call("auth.test", post=False)

    # -- channels -------------------------------------------------------

    def find_channel(self, name: str) -> dict | None:
        cursor = None
        while True:
            r = self.call("conversations.list", {
                "limit": 1000, "exclude_archived": True,
                "types": "public_channel,private_channel", "cursor": cursor,
            }, post=False)
            for ch in r.get("channels", []):
                if ch.get("name") == name:
                    return ch
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None

    def ensure_channel(self, name: str, purpose: str = "") -> dict:
        """Create the channel, or return it if it already exists. Joins it."""
        name = re.sub(r"[^a-z0-9_-]", "-", name.lower())[:80].strip("-")
        try:
            ch = self.call("conversations.create", {"name": name, "is_private": False})["channel"]
            created = True
        except SlackError as e:
            if e.error not in ("name_taken", "channel_already_exists"):
                raise
            found = self.find_channel(name)
            if not found:
                raise
            ch, created = found, False

        try:
            self.call("conversations.join", {"channel": ch["id"]})
        except SlackError:
            pass  # already a member, or private channel we were invited to

        if created and purpose:
            try:
                self.call("conversations.setPurpose",
                          {"channel": ch["id"], "purpose": purpose[:250]})
            except SlackError:
                pass

        ch["_created"] = created
        return ch

    # -- messages -------------------------------------------------------

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> dict:
        return self.call("chat.postMessage", {
            "channel": channel, "text": text, "thread_ts": thread_ts,
            "unfurl_links": False, "unfurl_media": False, "mrkdwn": True,
        })

    def update(self, channel: str, ts: str, text: str) -> dict:
        return self.call("chat.update", {
            "channel": channel, "ts": ts, "text": text, "link_names": False,
        })

    def channel_info(self, channel_id: str) -> dict:
        return self.call("conversations.info", {"channel": channel_id},
                         post=False)["channel"]

    def history(self, channel: str, oldest: str | None = None, limit: int = 200) -> list[dict]:
        out, cursor = [], None
        while True:
            r = self.call("conversations.history", {
                "channel": channel, "oldest": oldest, "limit": min(limit, 200),
                "cursor": cursor,
            }, post=False)
            out.extend(r.get("messages", []))
            if not r.get("has_more") or len(out) >= limit:
                break
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return sorted(out, key=lambda m: float(m.get("ts", 0)))

    def replies(self, channel: str, thread_ts: str) -> list[dict]:
        r = self.call("conversations.replies",
                      {"channel": channel, "ts": thread_ts, "limit": 200}, post=False)
        return r.get("messages", [])

    def user_name(self, user_id: str, _cache={}) -> str:
        if not user_id:
            return "unknown"
        if user_id in _cache:
            return _cache[user_id]
        try:
            u = self.call("users.info", {"user": user_id}, post=False)["user"]
            name = u.get("profile", {}).get("display_name") or u.get("real_name") or u.get("name")
        except SlackError:
            name = user_id
        _cache[user_id] = name
        return name


# ----------------------------------------------------------------------
# markdown -> Slack mrkdwn
# ----------------------------------------------------------------------

def to_mrkdwn(md: str) -> str:
    """Slack's mrkdwn is not markdown. Convert the parts that differ."""
    out = []
    for line in md.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            out.append(f"*{m.group(2).strip()}*")
            continue
        # Order matters: park **bold** behind a sentinel so the *italic* pass
        # below cannot re-match the single asterisks it would leave behind.
        line = re.sub(r"\*\*(.+?)\*\*", "\x00\\1\x00", line)
        line = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"_\1_", line)
        line = line.replace("\x00", "*")
        line = re.sub(r"^(\s*)-\s+\[ \]\s+", r"\1• ☐ ", line)    # unchecked task
        line = re.sub(r"^(\s*)-\s+\[x\]\s+", r"\1• ☑ ", line, flags=re.I)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)           # bullets
        line = re.sub(r"\[(.+?)\]\((\S+?)\)", r"<\2|\1>", line)  # links
        line = re.sub(r"^---+$", "", line)                       # rules
        out.append(line)
    return "\n".join(out).strip()


def chunk(text: str, limit: int = 2900) -> list[str]:
    """Split on paragraph/line boundaries so no Slack message exceeds `limit`."""
    if len(text) <= limit:
        return [text]
    def hard_split(s: str) -> list[str]:
        """Last resort for a single line longer than the limit: split on a word
        boundary near the limit, or mid-word if there isn't one."""
        out = []
        while len(s) > limit:
            cut = s.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            out.append(s[:cut])
            s = s[cut:].lstrip()
        if s:
            out.append(s)
        return out

    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(para) > limit:                       # single huge paragraph
            for line in para.splitlines(keepends=True):
                for piece in (hard_split(line) if len(line) > limit else [line]):
                    if len(cur) + len(piece) > limit:
                        chunks.append(cur.rstrip()); cur = ""
                    cur += piece
            continue
        if len(cur) + len(para) + 2 > limit:
            chunks.append(cur.rstrip()); cur = ""
        cur += para + "\n\n"
    if cur.strip():
        chunks.append(cur.rstrip())
    return [c for c in chunks if c.strip()]


if __name__ == "__main__":
    try:
        s = Slack()
        me = s.whoami()
        print(f"OK  team={me.get('team')} ({me.get('team_id')})  "
              f"bot={me.get('user')} ({me.get('user_id')})")
    except SlackError as e:
        print(f"FAIL  {e}")
        raise SystemExit(1)
