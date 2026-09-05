# Running as a service

How to keep claude-spillway resident as a systemd user service, started
automatically when you log in.

[日本語版はこちら](service.ja.md)

For the parts specific to Windows + WSL2 — reaching the proxy from the
Windows-side VSCode, when the distro actually boots — see
[wsl-windows.md](wsl-windows.md).

## Why bother

While `ANTHROPIC_BASE_URL` points at this proxy, **every Claude Code session
loses connectivity if the proxy is down**. Starting it by hand or from a login
script gives you no way to notice that, and nothing brings it back. A systemd
user service gives you:

- automatic restart via `Restart=always`
- start on login
- logs through `journalctl`
- the API key kept out of the config file, via `EnvironmentFile`

## Install

```bash
# 1. install the tool
uv tool install claude-spillway

# 2. register it as a service
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
./scripts/install-service.sh
```

The script:

1. creates `~/.config/claude-spillway/` and puts the example config there
2. asks for your Ollama Cloud API key and writes it to
   `~/.config/claude-spillway/env` with `chmod 600`
3. installs the unit into `~/.config/systemd/user/`, resolving `ExecStart` to
   the absolute path of the executable on this machine
4. runs `systemctl --user enable --now` and waits for the proxy to answer

**It is idempotent.** Re-running never overwrites an existing `config.yaml` or
`env`, so you can run it again after upgrading just to refresh the unit.

To skip the prompt, export the key first:

```bash
OLLAMA_API_KEY=your-key ./scripts/install-service.sh
```

### After installing

Point Claude Code at the proxy in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

`API_FORCE_IDLE_TIMEOUT=0` lifts the 5-minute streaming idle timeout, which is
worth setting because Ollama models tend to pause between chunks while in
fallback. See the [README](../README.md) for the rest.

## Day to day

```bash
systemctl --user status claude-spillway      # is it running
systemctl --user restart claude-spillway     # restart
systemctl --user stop claude-spillway        # stop
journalctl --user -u claude-spillway -f      # follow the logs
claude-spillway monitor                      # watch quota in a TUI
```

`monitor` also prints the address of the browser dashboard the proxy serves
(<http://127.0.0.1:8787/_spillway/>), which shows the same figures with reset
countdowns and threshold markers.

## Upgrading

```bash
uv tool upgrade claude-spillway
systemctl --user restart claude-spillway
```

`ExecStart` holds the absolute path of the executable, so the unit normally
needs no attention. Re-run `./scripts/install-service.sh` only if the install
location changed.

## Uninstall

```bash
./scripts/install-service.sh --uninstall
```

Stops and disables the service and removes the unit. **The config and API key
are kept**, so a re-install needs no retyping. To remove those too:

```bash
rm -rf ~/.config/claude-spillway
```

## Changing the config

The config lives at `~/.config/claude-spillway/config.yaml` — the very path
`serve` looks in when `-c` is omitted, so editing it and restarting is all it
takes.

```bash
$EDITOR ~/.config/claude-spillway/config.yaml
systemctl --user restart claude-spillway
```

Every field is documented in [config.example.yaml](../config.example.yaml).

### Sharing it across machines

`config.yaml` holds no secrets — the API key stays in the separate `env` file —
so it can live in a dotfiles repository and be symlinked into place:

```bash
ln -s ~/dotfiles/claude-spillway/config.yaml ~/.config/claude-spillway/config.yaml
```

Do this **before** running the installer, which leaves an existing
`config.yaml` alone, symlink included.

The config does not follow upgrades. After `uv tool upgrade`, diff it against
[config.example.yaml](../config.example.yaml) to pick up options added since
you installed:

```bash
diff <(sed -n '/^listen:/,$p' ~/.config/claude-spillway/config.yaml) \
     <(sed -n '/^listen:/,$p' config.example.yaml)
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `systemctl not found` | systemd is not running. On WSL, put `[boot] systemd=true` in `/etc/wsl.conf` and run `wsl --shutdown` |
| `claude-spillway not found` | Run `uv tool install claude-spillway` first |
| `address already in use` right after starting | A manually started instance is still around. Find it with `ss -tlnp \| grep 8787` and stop it |
| Auth errors only after a failover | `OLLAMA_API_KEY` never reached the service. Check `systemctl --user show claude-spillway -p Environment` and `~/.config/claude-spillway/env` |
| `monitor` keeps showing "waiting" | Expected. The proxy holds no Anthropic credentials of its own — it borrows the one Claude Code sends — so nothing shows until one real request has gone through |
| It stops when you log out | User services follow your login session by default. To keep it resident from boot, run `sudo loginctl enable-linger $USER` |

## About the unit file

The unit that gets installed is
[packaging/systemd/claude-spillway.service](../packaging/systemd/claude-spillway.service);
start from that if you would rather write it yourself. Two things matter:

- **`Restart=always` / `RestartSec=5`** — close to mandatory, since the proxy
  going down takes all of Claude Code with it.
- **`EnvironmentFile`** — keeps the API key out of the config file. Leave
  `api_key: ${OLLAMA_API_KEY}` in `config.yaml` and it is expanded from the
  process environment.
