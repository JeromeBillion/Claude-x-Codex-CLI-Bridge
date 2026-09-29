# Windows personal alpha launch and acceptance

This source ZIP uses only Python's standard library. It is built from a clean
commit with `py -3 -m tools.package_windows_alpha`, then verified against its
SHA-256 manifest. It does not bundle Python, Codex, Claude Code or their
credentials. There is no new paid software or API-key setup.

## Open the alpha

1. Install Python 3.11 or newer and the official `codex` and `claude` CLIs;
   sign in with each provider's own CLI. Keep their auth files with the CLIs.
2. Extract `ClaudeCodexDesktop-alpha-<revision>.zip` to a normal local folder.
3. Double-click `launch-desktop.cmd` inside `ClaudeCodexDesktop-alpha`, or run
   it from Command Prompt. It checks Python and opens the Tk desktop. Starting
   the window makes no model call.
4. Choose a disposable local coding project, select a provider mode, and click
   **Connect / refresh catalogs**. Confirm that account and model choices
   appear before sending any turn. The host must ask before credit-billed
   Fable use and before trusting a folder for automatic edits.

The existing `bridge.ps1` CLI remains in the archive. From PowerShell, it can
still be launched separately; the desktop and CLI have distinct sessions.

## Windows acceptance gate

This packaging step is preparation, **not a completed release claim**. Before
labeling the product ready for Jerome's Windows use, exercise two real turns
and native resume for each provider, default per-edit decline and accept,
trusted-folder automatic edits, native sandbox boundaries, collaboration
agreement/disagreement, a real or simulated limit-to-handoff recovery, and
failure diagnostics in a disposable project. Confirm the extracted archive
itself runs after the reviewed PRs are merged. Record CLI versions and safe
results in VLI-157–163 and VLI-162 without credentials or private transcripts.

Mac support is a later roadmap phase tracked in VLI-184, after Windows alpha
acceptance.
