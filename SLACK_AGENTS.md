# Using the shared Slack channel — guide for agents

This channel carries two streams that share one timeline:

1. **Live meeting notes** — posted automatically from a laptop recording the
   room (mic + system audio → local Whisper → Haiku-organized notes).
2. **Agent coordination** — structured updates that agents post and read so
   parallel work on the same project does not collide.

If you are an agent working on this project, read this file once, then work
from `COORDINATION.md`.

---

## 1. Access

You need a Slack **bot token** (`xoxb-…`) for the workspace, with these scopes:

| Scope | Why |
|---|---|
| `chat:write` | post updates |
| `channels:read` | find the channel |
| `channels:history` | read what other agents posted |
| `channels:join` | join the channel |
| `channels:manage` | only needed by whoever creates the channel |

Set it as `SLACK_BOT_TOKEN` in your environment. Do **not** paste tokens into
messages, prompts, commit history, or the coordination doc — the channel is
readable by everyone in the workspace and message history is durable.

Verify access:

```bash
python C:\Users\globa\notetaker\slack_client.py
# OK  team=<workspace> (T0BKRHTCC4W)  bot=<name> (U…)
```

---

## 2. The coordination document

`C:\Users\globa\notes\COORDINATION.md`

This is the file to read **before you start work**. It is regenerated from the
channel on every sync, so it always reflects what every agent has posted. It
contains:

- **Active agents** — who is working, on what, last seen when
- **Claimed / in progress** — tasks someone already owns
- **Blocked** — work that stalled and why
- **Decisions** — choices already made (do not relitigate them)
- **Open questions** — unresolved, fair game to answer
- **Completed** — finished work
- **Latest meeting notes** — what the humans in the room actually said

It is **generated**. Hand edits are overwritten on the next sync. To change
what it says, post to the channel.

Refresh it yourself before relying on it:

```bash
python C:\Users\globa\notetaker\slack_reader.py --once
```

Or keep it continuously current:

```bash
python C:\Users\globa\notetaker\slack_reader.py --watch --interval 60
```

---

## 3. Posting an update

Post a fenced block tagged `agent` containing one JSON object. Slack renders it
as a code block for humans; the reader parses it for machines.

````
🔒 *impl-alpha* — claim: camera calibration
```agent
{"agent": "impl-alpha", "type": "claim", "task": "camera calibration",
 "note": "starting now, touching vision/calib.py", "project": "arm-skills"}
```
````

Fields:

| Field | Required | Meaning |
|---|---|---|
| `agent` | yes | your unique name, `role-qualifier` (e.g. `impl-alpha`) |
| `type` | yes | one of the types below |
| `task` | for claim/done/blocked | short stable task name — the key others match on |
| `note` | recommended | one line of detail |
| `project` | recommended | which project/repo this is |

Types:

| Type | Post it when |
|---|---|
| `claim` | you are about to start a task — **before** you touch files |
| `done` | the task is finished and verified |
| `blocked` | you stopped and need something; put the reason in `note` |
| `status` | periodic "still working, here's where I am" |
| `question` | you need a human or another agent to answer something |
| `decision` | a choice was made that others must not relitigate |
| `note` | anything else worth the room knowing |

Easiest way to post:

```bash
python C:\Users\globa\notetaker\slack_reader.py \
  --agent impl-alpha --project arm-skills \
  --task "camera calibration" \
  --post claim "starting now, touching vision/calib.py"
```

Or from your own code:

```python
import sys; sys.path.insert(0, r"C:\Users\globa\notetaker")
from slack_client import Slack
from slack_reader import post_update

slack = Slack()
channel = slack.ensure_channel("notes-live")
post_update(slack, channel["id"], "impl-alpha", "done",
            task="camera calibration", note="tests pass", project="arm-skills")
```

---

## 4. Rules of engagement

1. **Claim before you work.** Read `COORDINATION.md` first. If a task is
   already claimed by another agent, pick something else or coordinate in the
   channel. A duplicate claim means two agents editing the same files.
2. **Post `done` or `blocked` when you stop.** An open claim that never closes
   makes the task look owned when nobody is working it.
3. **Respect `decision` entries.** If you disagree with a recorded decision,
   post a `question` — do not silently implement the other option.
4. **Meeting notes are lossy.** They come from speech-to-text on far-field room
   audio. Terms marked `(?)` are the transcriber's best guess. Never treat a
   number, name, or commitment from the notes as authoritative — confirm it
   before acting on it.
5. **One task per claim.** "refactor everything" is not a claimable task.
6. **Keep secrets out.** No tokens, keys, or credentials in messages.

---

## 5. Reading the channel directly

If you would rather parse the channel yourself:

```python
import sys; sys.path.insert(0, r"C:\Users\globa\notetaker")
from slack_client import Slack
from slack_reader import parse_agent_blocks

slack = Slack()
ch = slack.ensure_channel("notes-live")
for m in slack.history(ch["id"], limit=200):
    for block in parse_agent_blocks(m.get("text", "")):
        print(block["agent"], block["type"], block.get("task"))
```

`slack.history()` returns messages oldest-first and follows pagination.

---

## 6. Components

| Path | Role |
|---|---|
| `C:\Users\globa\notetaker\notetaker.py` | records audio, writes transcript + notes |
| `C:\Users\globa\notetaker\slack_poster.py` | posts notes to the channel on an interval |
| `C:\Users\globa\notetaker\slack_reader.py` | channel → `COORDINATION.md`; also posts updates |
| `C:\Users\globa\notetaker\slack_client.py` | Slack Web API wrapper (stdlib only) |
| `C:\Users\globa\notes\COORDINATION.md` | the shared document (generated) |
| `C:\Users\globa\notes\<session>\` | per-session `transcript.md` + `notes.md` |
