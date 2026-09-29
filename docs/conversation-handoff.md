# Shared conversation and curated handoff (VLI-160)

Status: implemented in `tools/conversation.py` and wired into `tools/desktop.py`.
It is tested with fake turns and a real Tk window. No provider turn has been run
with it. This is a personal Windows tool: the record stays on Jerome's PC.

## Contract

### 1. One shared conversation per project

- The desktop keeps its own append-only JSONL log. Each line holds `v`, `seq`,
  `at`, `kind`, `provider`, `session_ref` and `data`.
- The log lives at
  `%LOCALAPPDATA%\ClaudeCodexDesktop\conversations\<sha256(workspace)[:16]>\<uuid>.jsonl`.
  The folder name is a hash, so it does not reveal the project path.
- Choosing a project reopens its newest conversation.
- The **Conversation** window shows it, with three actions: *Export redacted
  copy*, *Start new conversation* and *Forget this conversation*. Forget
  deletes only the desktop's record, never a provider's own session.

Entry kinds:

| Kind | Content |
| --- | --- |
| `user_message` | The text, the mode and the providers it went to. |
| `turn` | One entry per finished provider turn: role, model, text (last 60,000 characters, with a truncation flag), `ok` and failure, tools with their error state, and each approval request with its policy, decision and who decided. Also limit, notice, error and exit signals, with scalar fields only. |
| `handoff` | The exact packet sent, source, target, reason, redaction counts, and which items were included or omitted. |
| `collaboration` | State, lead and each provider's vote. |
| `user_decision` | What the user chose when the providers did not jointly approve. |
| `note` | Host notices. |

Robustness:
- A torn last line (a crash mid-write) or a corrupt line is skipped and counted
  in `damaged_lines`. The inspector shows the count. The rest of the history
  still loads, because refusing all of it would be worse.
- Each append is flushed and fsynced. The UI thread and the turn worker share
  one lock.

### 2. Native session IDs never mix

- `session_ref` is only ever stored with the `provider` that issued it.
  `append()` refuses a `session_ref` that names no provider.
- `native_sessions()` returns `{"claude": [...], "codex": [...]}`, which is
  never merged.
- A handoff shows only the **source** provider's IDs, labeled "these resume
  only in <source>; this is a new <target> conversation, not a session
  transfer". It never gives the target an ID, whether the target's own or the
  source's.
- No consumer-chat import and no cross-provider native transfer is claimed.

### 3. Curated, user-editable handoff

`draft_handoff(log, source=, target=, user_summary=, selected_files=, file_diffs=, reason=)`
builds items from the log:

| Item | Default | Notes |
| --- | --- | --- |
| Summary from the user | included | Required; cannot be left out. |
| Latest user request | included | |
| Source's last turn (verdict) | included | |
| Tools it used | included | |
| Approval decisions | included | |
| Source's reply (tail) | included | Bounded. |
| Files the user selected | included | |
| One item per file diff | **off** | Opt-in. |

What the user can do:
- Tick any item except the summary on or off, and edit the final text freely.
- See items that were left out named in the packet ("Left out by the user:
  ..."), so the receiver knows context is missing.

Reason: the reason is taken from the source's last turn. It is "the source
provider reached its usage limit" when the turn has `rate_limit`
`status=rejected` or a `runtime_error` `category=limit`, "last turn failed
(...)" when the turn failed for another reason, and "switching provider"
otherwise. This drives the **limit-to-handoff** path: a failed or limited turn
shows a hint in the timeline to use **Hand off...**.

**Hand off...** does not send anything. It sets the target's single-provider
mode and puts the reviewed packet into the prompt box, and the user presses
Send. The packet is recorded as a `handoff` entry.

### 4. Redaction

The rendered packet is redacted before the user sees it. Each match is replaced
by `[REDACTED:<category>]`:
- Anthropic, OpenAI, GitHub, AWS access-key, Google API-key, Slack and Stripe tokens
- JWTs, bearer tokens and private-key blocks
- `user:pass@` in URLs
- `*PASSWORD*`, `*SECRET*`, `*TOKEN*`, `*API_KEY*`, `*ACCESS_KEY*` and similar assignments
- e-mail addresses

Home-folder names are rewritten to `C:\Users\<user>`.

Only categories and counts are reported, never the matched text.

Size: the packet is capped at 8,000 characters by default. The shortening is
stated in the packet and counted as `shortened`.

Final check: `finalize()` scans the user's edited text **again** before
sending. If the text looks secret, the dialog names the categories and asks
"Send it anyway, unredacted?".
- No: the dialog stays open.
- Yes: the text is sent as the user wrote it, and the counts are recorded.

The local log itself keeps the user's own text unredacted. It is his private
record, like the CLIs' own transcripts. *Export redacted copy* redacts the
whole render.

### 5. Collaboration (VLI-163 semantics unchanged)

- The curated packet is the **context**. The candidate answer goes separately,
  **verbatim**, under "Candidate answer (exact text under review)", together
  with its SHA-256.
- Both providers must approve that exact text, so it is never trimmed or
  redacted. If it contains secret-like content, the dialog says so.
- The previous `handoff_text()` sent only the last 4,000 characters, so a
  reviewer could approve a digest of text it never saw. That gap is closed.
- Joint approval still requires both exact approvals.
- A disagreement, a failure or a cancelled handoff means `needs_user_decision`.
  The user's choice is recorded.

## Tests

- `tools/tests/test_conversation.py`: redaction of every category without
  leaking values, ordinary code left alone, persistence and reopen, hashed
  folder, native-ID separation, torn and corrupt lines, redacted export,
  forget, edit and omit, opt-in diffs, required summary, the re-scan before
  sending, limit reason, the size bound, and a refused same-provider handoff.
- `tools/tests/test_desktop_conversation.py`:
  - collaboration sends the candidate verbatim while the context is redacted
  - a cancelled handoff stays unapproved
  - the real Tk dialog: manual handoff prefills without sending; a pasted token is caught and names only its category; the Conversation window lists native sessions per provider

## Open

- Real turns have not been run through this path on Jerome's PC. That is part
  of Windows acceptance.
- Regex redaction is best effort. Unusual secret formats can get through; the
  user's review and the final re-scan are the backstop.
- File diffs are passed in by the caller. The dialog does not yet have a
  picker for the selected files.
