# Running on WSL2 with Windows VSCode

How to keep claude-spillway resident inside WSL2 and use it from the Claude Code
extension in VSCode on Windows.

[日本語版はこちら](wsl-windows.ja.md)

## Layout

```
Windows                              WSL2 (Ubuntu)
┌────────────────────────┐          ┌─────────────────────────────┐
│ VSCode (native)        │          │ VSCode Server (Remote-WSL)  │
│  └ Claude Code ext.    │          │  └ Claude Code ext.         │
│      win32-x64         │          │      linux-x64              │
│         │              │          │         │                   │
│  %USERPROFILE%\.claude\│          │  ~/.claude/settings.json    │
│    settings.json       │          │         │                   │
└─────────┼──────────────┘          └─────────┼───────────────────┘
          │  http://127.0.0.1:8787            │
          │  (localhost forwarding)           │
          └──────────────┬───────────────────-┘
                         ▼
              claude-spillway (systemd user service)
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        Anthropic API         Ollama Cloud
                              (when quota is low)
```

The important part: **keep the proxy bound to `127.0.0.1` — Windows still
reaches it.** WSL2's localhost forwarding takes care of it, so there is no need
to bind `0.0.0.0`. That matters here, because this proxy relays your auth
headers and should not be exposed to the LAN.

## Prerequisites

| Item | Required state | How to check |
|---|---|---|
| systemd | enabled | `ps -p 1 -o comm=` prints `systemd` |
| localhost forwarding | enabled (the default) | `%USERPROFILE%\.wslconfig` does not set `localhostForwarding=false` |

If systemd is not enabled, put this in `/etc/wsl.conf` and run `wsl --shutdown`:

```ini
[boot]
systemd=true
```

`networkingMode=mirrored` (Windows 11 only) is **not** required; the default NAT
mode works.

## 1. Install

```bash
# if you don't have uv yet
curl -LsSf https://astral.sh/uv/install.sh | sh

# install to a stable path (~/.local/bin/claude-spillway)
uv tool install git+https://github.com/akivajp/claude-spillway
# to run a local checkout instead:
#   uv tool install --editable ~/git/claude-spillway
```

## 2. Config file and API key

When `-c` is omitted, claude-spillway looks for a config file in this order:

1. the path in the `CLAUDE_SPILLWAY_CONFIG` environment variable
2. `~/.config/claude-spillway/config.yaml` (honours `$XDG_CONFIG_HOME`; on
   Windows, `%APPDATA%\claude-spillway\config.yaml`)

Using the standard location keeps the service unit short.

```bash
mkdir -p ~/.config/claude-spillway
curl -o ~/.config/claude-spillway/config.yaml \
  https://raw.githubusercontent.com/akivajp/claude-spillway/main/config.example.yaml

# keep the API key out of the config file
echo 'OLLAMA_API_KEY=your-ollama-cloud-api-key' > ~/.config/claude-spillway/env
chmod 600 ~/.config/claude-spillway/env
```

Leave `api_key: ${OLLAMA_API_KEY}` as-is in `config.yaml`; `${...}` is expanded
from the process environment.

## 3. Keep it running as a systemd user service

Create `~/.config/systemd/user/claude-spillway.service`:

```ini
[Unit]
Description=claude-spillway: quota-aware failover proxy for Claude Code
Documentation=https://github.com/akivajp/claude-spillway
After=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/claude-spillway/env
ExecStart=%h/.local/bin/claude-spillway serve
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-spillway

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-spillway
systemctl --user status claude-spillway
journalctl --user -u claude-spillway -f    # follow the logs
```

`Restart=always` is close to mandatory: while `ANTHROPIC_BASE_URL` points at the
proxy, **every Claude Code session loses connectivity if the proxy is down**.

### Why not cron (`@reboot`)?

It works, but it has three traps:

1. **`@reboot` does not mean "when Windows boots".** WSL does not start with
   Windows; the distro boots when you open a WSL terminal or when VSCode
   connects over Remote-WSL. That is when `@reboot` fires.
2. **cron does not read `.bashrc` / `.profile`.** `${OLLAMA_API_KEY}` expands to
   an empty string, so startup looks fine and then **authentication fails
   silently the moment a failover happens** — the hardest failure to notice.
3. **No supervision and no restart.** Nothing tells you the process died.

### (Optional) Start it right after Windows logon

WSL does not boot until something touches it. If you want the proxy resident
from logon, register a Windows scheduled task that merely wakes the distro;
systemd takes it from there.

```powershell
$action  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu-24.04 -e true"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "wsl-boot" -Action $action -Trigger $trigger `
    -Description "Boot the WSL distro so its systemd services start"
```

If you always work through Remote-WSL, the distro boots on connect and this task
is unnecessary.

## 4. Point Claude Code at the proxy

**You do not need to set an OS environment variable (`setx`).** Use the `env`
key in `settings.json`: it takes precedence over shell environment variables and
is a one-line edit to undo.

The catch is that it must live in the home directory **of the side the extension
runs on**. If you use both the Windows and the WSL extension, you need both.

**WSL side** (Remote-WSL windows) — `~/.claude/settings.json`

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

**Windows side** (native windows) — `%USERPROFILE%\.claude\settings.json`

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

The URL is the same on both sides. To scope it per project, use
`.claude/settings.json` (shared) or `.claude/settings.local.json` (yours only).

### Notes on those variables

- `API_FORCE_IDLE_TIMEOUT=0` lifts the 5-minute streaming idle timeout.
  Recommended, because Ollama Cloud models tend to pause between chunks while in
  fallback.
- `ENABLE_TOOL_SEARCH=true` — pointing `ANTHROPIC_BASE_URL` at a non-first-party
  host disables MCP tool search by default. **Turning it back on is
  recommended** if you use many MCP servers, because it changes how much context
  the tool definitions eat.

  MCP still works with it off (all tool definitions are simply sent upfront),
  but measurements against Ollama Cloud (2026-09, `glm-5.3-flash:cloud`) show
  every tool-search construct is accepted:

  | What was sent | Ollama Cloud's response |
  |---|---|
  | Plain `tools` | HTTP 200, tool called |
  | With `defer_loading: true` | HTTP 200, tool called |
  | `tool_search_tool_regex_20251119` + `defer_loading` | HTTP 200, tool called |
  | `tool_reference` block in the history | HTTP 200, tool called |

  Ollama silently ignores `defer_loading`, `tool_search_tool_*` and
  `tool_reference` while still calling tools correctly. Note the flip side:
  **because `defer_loading` is ignored, every tool definition reaches the model
  while in fallback** — see "Choosing a fallback model" below.
- **Remote Control is disabled** whenever `ANTHROPIC_BASE_URL` points away from
  `api.anthropic.com` (Claude Code v2.1.196+). There is no way around it.

## Choosing a fallback model

Claude Code is an agent: a model that cannot call tools is effectively useless
to it. Every `target` in `model_mapping` **must be a tool-calling model**.

Check one by asking Ollama Cloud directly and looking for a `tool_use` block:

```bash
curl -s https://ollama.com/v1/messages \
  -H "authorization: Bearer $OLLAMA_API_KEY" \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"MODEL-TO-CHECK","max_tokens":128,
       "tools":[{"name":"get_weather","description":"Get the weather",
                 "input_schema":{"type":"object","properties":{"location":{"type":"string"}},
                                 "required":["location"]}}],
       "messages":[{"role":"user","content":"What is the weather in Tokyo? Use the tool."}]}' \
  | python3 -c "import json,sys; print([b.get('name') for b in json.load(sys.stdin)['content'] if b.get('type')=='tool_use'])"
```

`['get_weather']` means it works. An empty list means the model is unusable as a
fallback target.

Watch the **context length** too. With many MCP servers, every tool definition
reaches the fallback model even with tool search on, because Ollama ignores
`defer_loading`. Ollama's own docs recommend at least 64K for Claude Code
workloads; `config.example.yaml` notes the context length of each mapped model.

> Known risk: Ollama Cloud has been reported to return 500 errors on very large
> requests (18KB+ system prompts, 20+ tools). If failover fails on an
> MCP-heavy setup, check the "last error" row in `claude-spillway monitor`.

## 5. Verify

```bash
# from inside WSL
curl -s http://127.0.0.1:8787/_spillway/status | python3 -m json.tool

# from Windows (runnable from inside WSL)
/mnt/c/Windows/system32/curl.exe -s -o /dev/null -w "HTTP %{http_code}\n" \
  http://127.0.0.1:8787/_spillway/status

# is it really reaching Anthropic? An unauthenticated 401 proves it is,
# and consumes no quota.
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"x"}]}'
```

Live TUI:

```bash
claude-spillway monitor
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Startup fails with `address already in use` | A manually started instance is still around. Find it with `ss -tlnp \| grep 8787` and stop it |
| Claude Code cannot connect at all | The proxy is down. Check `systemctl --user status claude-spillway`. While `ANTHROPIC_BASE_URL` is set, the proxy is Claude Code's lifeline |
| Auth errors only after a failover | `OLLAMA_API_KEY` never reached the service. Check `systemctl --user show claude-spillway -p Environment` and the `EnvironmentFile` path |
| Reachable from WSL but not from Windows | Make sure `%USERPROFILE%\.wslconfig` does not set `localhostForwarding=false`. If it still fails, `wsl --shutdown` and retry |
| `monitor` keeps showing "waiting" | Expected. The proxy holds no Anthropic credentials and can only read quota from the response headers of real traffic, so nothing shows until one request has gone through |

## Known limitations

- **`wsl --shutdown` stops the proxy too.** systemd starts it again the next
  time the distro boots.
- **A resident process keeps the WSL VM alive.** The proxy itself is only about
  40 MB, but if you want the memory back, `systemctl --user stop
  claude-spillway` before `wsl --shutdown`.
- A user service starts with your first login session. To have it running from
  distro boot without a login, run `sudo loginctl enable-linger $USER` (not
  needed if you work through Remote-WSL).
