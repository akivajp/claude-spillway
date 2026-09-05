# claude-spillway

[日本語版 README はこちら](README.ja.md)

A quota-aware failover proxy for [Claude Code](https://claude.com/claude-code).

`claude-spillway` sits between Claude Code and Anthropic's API. It forwards
requests to Anthropic as normal, but watches the rate-limit headers on every
response. When your quota (the rolling 5-hour / 7-day usage window on a
Claude Pro/Max/Team subscription, or the classic request/token limits on an
API key) runs low, it automatically "spills over" `POST /v1/messages` traffic
to [Ollama Cloud](https://ollama.com) instead — and switches back to
Anthropic once quota recovers.

It was built to solve a very specific problem: you're paying for both a
Claude subscription and Ollama Cloud, and you'd rather burn through your
(often cheaper, more relaxed) Ollama quota automatically instead of getting
hard-blocked by Anthropic rate limits mid-session.

## How it works

```
Claude Code --ANTHROPIC_BASE_URL--> claude-spillway --> Anthropic API
                                          |
                                          '--(quota low)--> Ollama Cloud
                                                             (Anthropic-
                                                              compatible
                                                              /v1/messages)
```

- Every response from Anthropic carries rate-limit headers
  (`anthropic-ratelimit-unified-5h-utilization`,
  `anthropic-ratelimit-unified-7d-utilization` for subscription plans, or
  `anthropic-ratelimit-{requests,tokens}-{limit,remaining}` for API-key
  billing). claude-spillway parses these on every call — no extra API calls
  needed to know your usage.
- When the worst remaining ratio across all known signals drops below
  `fallback_threshold_pct` (default 10%), subsequent `POST /v1/messages`
  calls are routed to Ollama Cloud instead, with the model name rewritten
  per your `model_mapping` config.
- Requests to Anthropic don't need credentials configured in
  claude-spillway: whatever `Authorization`/`x-api-key` header Claude Code
  sends is forwarded as-is. Only Ollama Cloud needs an API key in the config
  (Ollama's Anthropic-compatible endpoint only accepts `Authorization:
  Bearer`, not `x-api-key` — see
  [ollama/ollama#16922](https://github.com/ollama/ollama/issues/16922)).
- While in fallback mode, a background probe periodically pings Anthropic
  with a minimal request to detect recovery. Once the remaining ratio climbs
  back above `recovery_threshold_pct` (default 20%, intentionally higher
  than the fallback threshold to avoid flapping), traffic switches back to
  Anthropic.

### Why only `/v1/messages` fails over

Only the actual inference call (`POST /v1/messages`) is subject to
failover. Auxiliary endpoints such as `/v1/messages/count_tokens` or model
listing always go to Anthropic, never to Ollama. This is deliberate: Ollama
Cloud's Anthropic-compatibility shim is known to hang and even restart the
server when it receives requests to endpoints it doesn't support
([ollama/ollama#13949](https://github.com/ollama/ollama/issues/13949)),
which would be far worse than just missing a token count.

### A note on Ollama Cloud quota visibility

As of 2026-09, Ollama Cloud has **no official API to check your remaining
account quota** ([ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663),
[#16448](https://github.com/ollama/ollama/issues/16448)). claude-spillway
can't show you an exact "Ollama remaining %" the way it can for Anthropic.
Instead, the status endpoint and TUI report self-tracked counters (requests
relayed, failures, last status code) for the traffic that passed through
this proxy.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
uv sync
```

## Quick start

1. Copy the example config and fill in your Ollama Cloud API key
   (get one at <https://ollama.com/settings/keys>):

   ```bash
   cp config.example.yaml config.yaml
   export OLLAMA_API_KEY=your-ollama-cloud-api-key
   ```

2. Start the proxy:

   ```bash
   uv run claude-spillway serve -c config.yaml
   ```

3. Point Claude Code at it and launch as usual:

   ```bash
   export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
   claude
   ```

4. (Optional) In another terminal, watch quota status live:

   ```bash
   uv run claude-spillway monitor
   ```

   claude-spillway never holds Anthropic credentials of its own — it only
   learns your quota by reading the rate-limit headers on responses to
   requests Claude Code actually sends through it. So `monitor` shows a
   "waiting" message until at least one real request has passed through;
   it can't proactively poll Anthropic on its own.

You can try the whole flow without any real credentials using the bundled
fake upstream servers — see
[`scripts/manual_smoketest/`](scripts/manual_smoketest/).

Running on Windows with WSL2? See
[docs/wsl-windows.md](docs/wsl-windows.md) for keeping the proxy resident as a
systemd user service and wiring up both the Windows and the WSL Claude Code
extension.

## Configuration

See [`config.example.yaml`](config.example.yaml) for the full reference.

When `-c` is omitted, the config file is looked up in this order:

1. the path in the `CLAUDE_SPILLWAY_CONFIG` environment variable
2. `~/.config/claude-spillway/config.yaml` (honours `$XDG_CONFIG_HOME`; on
   Windows, `%APPDATA%\claude-spillway\config.yaml`)

If neither exists, claude-spillway starts on its built-in defaults. Key fields:

| Field | Default | Description |
|---|---|---|
| `listen.host` / `listen.port` | `127.0.0.1` / `8787` | Where claude-spillway listens |
| `anthropic.base_url` | `https://api.anthropic.com` | Anthropic API endpoint |
| `ollama.base_url` | `https://ollama.com` | Ollama Cloud endpoint |
| `ollama.api_key` | — | Ollama Cloud API key (supports `${ENV_VAR}`) |
| `quota.fallback_threshold_pct` | `10.0` | Remaining % below which we fail over |
| `quota.recovery_threshold_pct` | `20.0` | Remaining % above which we switch back |
| `quota.probe_interval_seconds` | `60.0` | How often to probe Anthropic while in fallback |
| `model_mapping.rules` / `model_mapping.default` | — | Anthropic model name -> Ollama model name |

CLI flags on `claude-spillway serve` (`--host`, `--port`,
`--fallback-threshold-pct`, `--recovery-threshold-pct`, `--log-level`)
override the config file. Run `claude-spillway --help` /
`claude-spillway serve --help` / `claude-spillway monitor --help` for
details.

### Interface language

CLI help text and the `monitor` TUI are shown in English by default, and in
Japanese when the environment locale asks for it (`LC_ALL`, `LC_MESSAGES`,
`LANG` or `LANGUAGE` starting with `ja`). Set `CLAUDE_SPILLWAY_LANG` to
override the detection explicitly:

```bash
CLAUDE_SPILLWAY_LANG=en claude-spillway monitor  # force English
CLAUDE_SPILLWAY_LANG=ja claude-spillway monitor  # force Japanese
```

Log records emitted through `logging` are always in English, so they stay
easy to grep and to search for online.

## Development

```bash
uv run pytest      # unit + integration tests (no network access needed)
uv run ruff check . # lint
```

Tests exercise the full request/response path (including the mode switch
and hysteresis logic) against `httpx.MockTransport`, so they run offline and
don't touch real Anthropic/Ollama quota.

## Status

Early-stage, built for personal use and shared in case it's useful to
others. Not affiliated with Anthropic or Ollama.

## License

[MIT](LICENSE)
