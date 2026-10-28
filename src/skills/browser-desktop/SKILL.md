---
name: browser-desktop
description: Operate the paired browser computer with `ai-browser` and help users view its live desktop.
---

# Browser computer (browser-desktop)

You run inside the dev container. A separate paired browser container can provide
a visible Chromium desktop that you operate through `ai-browser`; the user can
view the same desktop through noVNC. You cannot operate the user's computer or
the Docker host through this skill.

## Check whether it is enabled

```bash
ai-browser status
```

- `state: idle` means the desktop stack is asleep (normal); the next operation wakes it.
- `state: starting` / `stopping` means it is waking or shutting down. Query `status`
  again later; a status query never wakes the desktop or extends the idle timer.
- `state: awake` with `control` / `cdp` means it is ready.
- `state: degraded` means a component is unhealthy; the next operation attempts
  recovery. If recovery fails, see the browser-container diagnostics below.
- `BROWSER_HOST is not set` means the browser address is not configured;
  `cannot reach browser computer` can also mean the container, network or
  service is unavailable. Neither message proves the browser was never enabled.
  Do not retry repeatedly; ask the Docker host administrator to check setup.

## Browser setup and diagnostics

The browser and its component logs are in the separate browser container, not
in the user's desktop environment. The agent in the dev container cannot run
host Docker commands. If recovery fails, ask the Docker host administrator to
inspect the browser container and its logs. The `host` field returned by
`ai-browser status` may help identify it, but can also be a hostname or IP
address. When the actual Docker container name is known, provide this command
for the Docker host:

```bash
docker logs --tail 80 <browser-container-name>
```

This shows container output; desktop component logs are under `/tmp/browser/`
inside the browser container. If necessary, the administrator can inspect those
with `docker exec` on the host. Ask for only relevant, redacted log excerpts.
If the container name is unavailable, ask the administrator to identify it;
never substitute the dev container or guess a user name or port base.

The host-side management scripts are in the project repository at
https://github.com/Tony-ooo/ai-dev-env, branch `main`
(`scripts/browser.sh` and `scripts/lib.sh`). A user who only pulled images may
not have a checkout. Refer them to that branch to obtain the repository when
needed, but first match the scripts to the deployed image's release or source
revision. The branch name alone does not establish a match; if the revision is
unknown, ask the deployment administrator or maintainer before recommending
that they run its management scripts. Do not assume the SSH account `dev` is
the deployment's `--user` value or invent the `--base` value.

For inspection, or once the branch is known to match, the host administrator
can obtain it with:

```bash
git clone --branch main --single-branch https://github.com/Tony-ooo/ai-dev-env.git
```

## Open the live desktop for the user

1. In the dev container, check `ai-browser status` and run `ai-browser screen`
   to obtain the SSH tunnel command and noVNC URL. You can use `ai-browser` to
   operate the paired browser, but cannot start a tunnel or open a browser on
   the user's computer. Neither command wakes the desktop; report it as awake
   only when the status actually says so.
2. Give only the following concise instructions, localized to the user's
   language. Include the full, copyable tunnel command, the actual noVNC URL,
   and the credential command; do not replace them with a generic description.
   The bracketed descriptions below are template markers, not literal output:

   1. On your computer, run the tunnel command and keep the terminal open.
      It prints when SSH connects; opening the live view checks the tunnel.

   ```bash
   [complete tunnel command from ai-browser screen, with the actual SSH address]
   ```

   2. On the same computer, open the noVNC URL.

   ```text
   [actual noVNC URL from ai-browser screen]
   ```

   3. In your dev container terminal, run this to see the login credentials:

   ```bash
   cat /home/dev/.ai-data/browser/state/screen-auth; printf '\n'
   ```

   Add: "If you have opened the live view before, try the address first; you
   may not need another tunnel. If it does not open, try a new tunnel using a
   different local port and use that same port in the address."

   Ask the user to confirm when the desktop is visible. Do not add explanations
   about credentials, SSH accounts, host addresses, ports, or troubleshooting
   unless they are needed for the user's actual situation or requested. If the
   SSH address is unknown, ask for it rather than presenting an unfilled
   placeholder as a command. If the viewing computer is the Docker host, use
   `127.0.0.1` when its SSH port is reachable locally. Do not run the credential
   command on the user's behalf, request its output, or expose credentials in chat. The
   user completes any MFA or CAPTCHA.
3. You can check container
   browser status and operate the shared desktop, but cannot independently
   observe the user's local browser or claim that their connection succeeded.
   If it fails, guide the user through checking their tunnel and login while
   checking the browser service from the container. `ai-browser shot` produces
   a still image, not a live view.

## Ask before operating the browser

Before the first browser operation in a task, ask the user in their preferred
language, using these choices:

> Would you like help opening the live browser view?
> 1. Yes, help me open the live view.
> 2. No, proceed without the live view.

Wait for the user's choice before operating the browser. If the user already
made this choice explicitly for the current task, do not ask again. For option
1, follow the live desktop workflow above and wait until the user confirms the
screen is visible before proceeding. For option 2, proceed directly. Do not
repeat the question for subsequent browser actions in the same task unless the
user changes their preference. Checking `ai-browser status` and retrieving
connection instructions with `ai-browser screen` do not operate the browser.

## Two ways to operate; prefer the text layer

| Layer | Commands | Use for |
|---|---|---|
| Text (preferred) | `open` / `navigate` / `snapshot` / `act` | Ordinary pages: read content, click buttons, fill forms |
| Pixels (fallback) | `click` / `type` / `key` / `scroll` / `shot` | iframes, captchas, native dialogs, when the text layer fails |
| Tabs | `tabs` / `close <id\|all>` | Fewer tabs wake faster after sleep |

The text layer works on the accessibility tree/DOM, so it locates elements precisely
without screenshots. **Try it first.**

## Text layer

```bash
ai-browser open https://example.com      # open a page (same URL reuses its tab)
ai-browser tabs                           # list tabs so you do not open duplicates
ai-browser close all                      # close extra tabs (keeps one)
ai-browser snapshot                       # page title, text, and refs of interactive elements
ai-browser act '[{"kind":"click","ref":"e3"}]'
ai-browser act '[{"kind":"fill","ref":"e1","text":"hello"}]'
ai-browser navigate https://example.org   # navigate the current tab
```

- A `ref` from `snapshot` (like `e3`) is **valid only for that snapshot**; take a new
  snapshot after the page changes.
- `act` runs a batch in order and reports how many steps succeeded in `completed`.
  When the response has `"uncertain": true`, **do not replay automatically**; take a
  snapshot first to see the current state.
- `Stale element ref` means the ref expired; take a new snapshot.

## Pixel layer

```bash
ai-browser shot -o /tmp/page.png         # full-screen screenshot, then read it with the file tools
ai-browser click 640 400                  # left click
ai-browser click 640 400 2                # double click
ai-browser type "search terms"
ai-browser type-keys "text"               # simulate individual keys when paste is inappropriate
ai-browser key ctrl+l                     # focus the address bar
ai-browser key Return
ai-browser scroll down 5
```

- The screenshot covers the **whole screen**, so take a `shot` before deciding
  coordinates; they are relative to the current resolution (1280x800).
- `type` goes to whatever has focus; click the field or press `key ctrl+l` first.
- Non-ASCII `type` replaces the desktop clipboard and sends Ctrl+V. It reports
  dispatch, not whether the application accepted the paste. Check the result;
  never automatically retry an uncertain action. Use `type-keys` when the
  application needs individual key events or treats Ctrl+V differently.
- When a human must log in, enter a 2FA code or solve a CAPTCHA, open the live
  desktop using the workflow above and let the user complete that step.

## Files

- Files the browser downloads land in `~/downloads` (shared both ways).
- `~/workspace` is **read-only** on the browser side. Use it to upload files or to
  preview build output over `file://`; never modify project files through the browser.

## Rules

1. Do not use Playwright or start a separate headless browser: it would diverge from
   the window the user sees and would not share their session.
2. After `open`, take a `snapshot` before acting; never click guessed coordinates.
3. Snapshot before a batch, then read `completed` / `uncertain` afterwards; snapshot
   again when unsure.
4. A `fallback` field in the response means the text layer could not handle it: take
   `ai-browser shot -o /tmp/p.png` to see the current screen, then act with
   `click <x> <y>`.
