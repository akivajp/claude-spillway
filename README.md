# claude-spillway

[![PyPI](https://img.shields.io/pypi/v/claude-spillway)](https://pypi.org/project/claude-spillway/)
[![Python](https://img.shields.io/pypi/pyversions/claude-spillway)](https://pypi.org/project/claude-spillway/)
[![License: MIT](https://img.shields.io/pypi/l/claude-spillway)](https://github.com/akivajp/claude-spillway/blob/main/LICENSE)

[日本語版 README はこちら](https://github.com/akivajp/claude-spillway/blob/main/README.ja.md)

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
- A background probe reads your quota from the OAuth usage endpoint — the one
  Claude Code itself reads for `/usage` — which **consumes no quota** and also
  reports when each window resets. Once the remaining ratio climbs back above
  `recovery_threshold_pct` (default 20%, intentionally higher than the
  fallback threshold to avoid flapping), traffic switches back to Anthropic.
  That endpoint is OAuth-only and is not part of the published API, so when it
  is unavailable the proxy falls back to the rate-limit headers of relayed
  traffic — and, only while in fallback where no traffic is flowing, to a
  minimal `/v1/messages` request that does cost a little quota.

### Why only `/v1/messages` fails over

Only the actual inference call (`POST /v1/messages`) is subject to
failover. Auxiliary endpoints such as `/v1/messages/count_tokens` or model
listing always go to Anthropic, never to Ollama. This is deliberate: Ollama
Cloud's Anthropic-compatibility shim is known to hang and even restart the
server when it receives requests to endpoints it doesn't support
([ollama/ollama#13949](https://github.com/ollama/ollama/issues/13949)),
which would be far worse than just missing a token count.

### A note on Ollama Cloud quota visibility

Ollama Cloud still publishes **no documented quota API**
([ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663),
[#16448](https://github.com/ollama/ollama/issues/16448)), and its inference
responses carry no rate-limit headers at all. But its own dashboard reads an
undocumented `GET /api/usage`, which takes the same API key as inference and is
not itself counted as a request — so claude-spillway polls that for the session
and weekly utilization plus a per-model request count.

Two caveats. It is undocumented, so a shape change degrades to "no data" rather
than an error. And unlike Anthropic, **Ollama reports no reset times**, so
nothing in this tool can tell you when an Ollama window turns over.

The status endpoint and TUI still also report self-tracked counters (requests
relayed, failures, last status code) covering only the traffic that passed
through this proxy.

### Choosing a backend

With both sides' quota visible, `routing.policy` decides between them while
neither is critical:

- `anthropic_first` (default) — stay on Anthropic until it runs low, then fail
  over. The original behaviour.
- `weekly_balance` — while both short windows are comfortable
  (`balance_session_floor_pct`), prefer whichever side has more of its **weekly**
  window left, switching only once the other side is ahead by
  `balance_margin_pct` so two near-equal backends don't oscillate.

Two guards apply under every policy:

- **Ollama exhaustion.** Never fail over into an Ollama account that is itself
  nearly out (`ollama_min_remaining_pct`) — that trades one dead end for another.
- **Reverse failover.** Ollama Cloud can get slow or fail outright when a model
  is busy. After `ollama_failure_threshold` consecutive failures, traffic goes
  back to Anthropic provided its 5-hour window still has
  `reverse_failover_min_5h_pct` left to serve with, and Ollama is left alone for
  `reverse_failover_cooldown_seconds`.

A hard guard outranks both: if Anthropic drops below
`quota.fallback_threshold_pct`, traffic fails over regardless of policy.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install claude-spillway
```

Or run it without installing:

```bash
uvx --from claude-spillway claude-spillway serve
```

To work on it instead, clone the repository:

```bash
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
uv sync
```

## Quick start

1. Put the example config in the location `serve` reads by default, and set
   your Ollama Cloud API key (get one at <https://ollama.com/settings/keys>):

   ```bash
   mkdir -p ~/.config/claude-spillway
   curl -o ~/.config/claude-spillway/config.yaml \
     https://raw.githubusercontent.com/akivajp/claude-spillway/main/config.example.yaml
   export OLLAMA_API_KEY=your-ollama-cloud-api-key
   ```

2. Start the proxy:

   ```bash
   claude-spillway serve
   ```

3. Point Claude Code at it and launch as usual:

   ```bash
   export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
   claude
   ```

4. (Optional) In another terminal, watch quota status live:

   ```bash
   claude-spillway monitor
   ```

   claude-spillway never holds Anthropic credentials of its own — it borrows
   the one Claude Code sends. So `monitor` shows a "waiting" message until at
   least one real request has passed through. After that it keeps polling on
   its own and stays live even while you are idle, because reading the usage
   endpoint costs no quota.

You can try the whole flow without any real credentials using the bundled
fake upstream servers — see
[`scripts/manual_smoketest/`](https://github.com/akivajp/claude-spillway/tree/main/scripts/manual_smoketest).

Running on Windows with WSL2? See
[docs/wsl-windows.md](https://github.com/akivajp/claude-spillway/blob/main/docs/wsl-windows.md) for keeping the proxy resident as a
systemd user service and wiring up both the Windows and the WSL Claude Code
extension.

## Configuration

See [`config.example.yaml`](https://github.com/akivajp/claude-spillway/blob/main/config.example.yaml) for the full reference.

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
| `quota.probe_interval_seconds` | `60.0` | How often the background probe refreshes the quota reading |
| `quota.use_usage_endpoint` | `true` | Read quota from the OAuth usage endpoint (consumes no quota) |
| `routing.policy` | `anthropic_first` | `anthropic_first` or `weekly_balance` |
| `routing.ollama_min_remaining_pct` | `5.0` | Never fail over once Ollama is this low |
| `routing.ollama_failure_threshold` | `5` | Consecutive Ollama failures that send traffic back |
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

## Support

If claude-spillway saves you from hitting a rate limit mid-session, you can
[buy me a coffee](https://buymeacoffee.com/akivajp).

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=flat-square&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/akivajp)

## License

[MIT](https://github.com/akivajp/claude-spillway/blob/main/LICENSE)
