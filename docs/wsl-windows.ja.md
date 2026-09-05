# WSL2 + Windows での常駐運用ガイド

Windows上のVSCode（Claude Code拡張）から、WSL2内で常駐させた claude-spillway を使うための構築手順です。

[English version](wsl-windows.md)

## 構成

```
Windows                              WSL2 (Ubuntu)
┌────────────────────────┐          ┌─────────────────────────────┐
│ VSCode (ネイティブ)     │          │ VSCode Server (Remote-WSL)  │
│  └ Claude Code 拡張     │          │  └ Claude Code 拡張         │
│      win32-x64          │          │      linux-x64              │
│         │               │          │         │                   │
│  %USERPROFILE%\.claude\ │          │  ~/.claude/settings.json    │
│    settings.json        │          │         │                   │
└─────────┼───────────────┘          └─────────┼───────────────────┘
          │  http://127.0.0.1:8787             │
          │  (localhostForwarding)             │
          └──────────────┬────────────────────-┘
                         ▼
              claude-spillway (systemd user service)
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
        Anthropic API         Ollama Cloud
                              (quota逼迫時)
```

ポイント: **プロキシは `127.0.0.1` にバインドしたままで、Windows側からも到達できます。** WSL2のlocalhost転送が効くためで、`0.0.0.0` へバインドする必要はありません（LANへの露出を避けられます。本プロキシは認証ヘッダーを中継するため、これは重要です）。

## 前提条件

| 項目 | 必要な状態 | 確認コマンド |
|---|---|---|
| systemd | 有効 | `ps -p 1 -o comm=` が `systemd` |
| localhost転送 | 有効（既定） | `%USERPROFILE%\.wslconfig` に `localhostForwarding=false` を書いていないこと |

systemdが無効な場合は `/etc/wsl.conf` に以下を書いて `wsl --shutdown` してください。

```ini
[boot]
systemd=true
```

`networkingMode=mirrored`（Windows 11のみ）は**不要**です。既定のNATモードで動作します。

## 1. インストール

```bash
# uv が未導入なら
curl -LsSf https://astral.sh/uv/install.sh | sh

# 固定パス (~/.local/bin/claude-spillway) にインストール
uv tool install claude-spillway
# 開発中のリポジトリをそのまま使う場合:
#   uv tool install --editable ~/git/claude-spillway
```

## 2. 設定ファイルとAPIキー

`-c` を省略した場合、claude-spillway は次の順で設定ファイルを探索します。

1. 環境変数 `CLAUDE_SPILLWAY_CONFIG` が指すパス
2. `~/.config/claude-spillway/config.yaml`（`$XDG_CONFIG_HOME` を尊重。Windowsでは `%APPDATA%\claude-spillway\config.yaml`）

サービス定義を短く保てるので、標準の場所に置くことを推奨します。

```bash
mkdir -p ~/.config/claude-spillway
curl -o ~/.config/claude-spillway/config.yaml \
  https://raw.githubusercontent.com/akivajp/claude-spillway/main/config.example.yaml

# APIキーは設定ファイルに直書きせず、環境変数ファイルに分離する
echo 'OLLAMA_API_KEY=あなたのキー' > ~/.config/claude-spillway/env
chmod 600 ~/.config/claude-spillway/env
```

`config.yaml` 側は `api_key: ${OLLAMA_API_KEY}` のままにしておきます（`${...}` はプロセス環境変数で展開されます）。

## 3. systemd ユーザーサービスとして常駐させる

`~/.config/systemd/user/claude-spillway.service` を作成します。

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
journalctl --user -u claude-spillway -f    # ログ追跡
```

`Restart=always` は必須級です。`ANTHROPIC_BASE_URL` を設定した状態でプロキシが停止すると、**すべてのClaude Codeセッションが接続不能**になるためです。

### なぜ cron (`@reboot`) を勧めないのか

動作はしますが、3つの落とし穴があります。

1. **`@reboot` は「Windows起動時」ではありません。** WSLはWindows起動時に自動では立ち上がらず、ターミナルを開くかVSCodeがRemote-WSL接続した時点で起動します。`@reboot` が発火するのはそのタイミングです。
2. **cronは `.bashrc` / `.profile` を読み込みません。** `${OLLAMA_API_KEY}` が空文字に展開され、起動は成功したように見えて**フェイルオーバーした瞬間に静かに認証失敗**します。最も気づきにくい壊れ方です。
3. **監視も自動再起動もありません。** プロセスが落ちても気づけません。

### （任意）Windows起動直後から常駐させる

WSLは何かがアクセスするまで起動しません。Windowsにログオンした時点で確実に常駐させたい場合は、Windows側のタスクスケジューラに「ディストロを起こすだけ」の軽いタスクを登録します。あとはsystemdが面倒を見ます。

```powershell
$action  = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d Ubuntu-24.04 -e true"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "wsl-boot" -Action $action -Trigger $trigger `
    -Description "Boot the WSL distro so its systemd services start"
```

VSCodeでRemote-WSLを使う運用であれば、接続時にディストロが起動するのでこのタスクは不要です。

## 4. Claude Code に接続先を教える

**OS環境変数（`setx`）を設定する必要はありません。** `settings.json` の `env` キーを使います。シェルの環境変数より優先され、1行の編集で切り戻せます。

注意点は、**Claude Code拡張が動いている側のホームディレクトリ**に置く必要があることです。Windows版とWSL版の両方の拡張を使っている場合は、両方に必要です。

**WSL側**（Remote-WSLウィンドウ用）— `~/.claude/settings.json`

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

**Windows側**（ネイティブウィンドウ用）— `%USERPROFILE%\.claude\settings.json`

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

URLは両方とも同じで構いません。プロジェクト単位で切り替えたい場合は `.claude/settings.json`（共有）や `.claude/settings.local.json`（自分専用）に書けます。

### 環境変数についての補足

- `API_FORCE_IDLE_TIMEOUT=0` — ストリーミングの5分アイドルタイムアウトを解除します。フォールバック中のOllama Cloudモデルはチャンク間隔が空きやすいため推奨です。
- `ENABLE_TOOL_SEARCH=true` — `ANTHROPIC_BASE_URL` が `api.anthropic.com` 以外を指すと、MCPのtool searchは既定で無効になります。MCPサーバーを多数使う場合はコンテキスト消費が大きく変わるため、**有効化を推奨します**。

  無効のままでもMCP自体は動作します（全ツール定義が前もって送られるだけ）が、Ollama Cloud側での実測（2026-09、`glm-5.3-flash:cloud`）では、tool search関連の構文はすべて問題なく受け付けられました。

  | 送信内容 | Ollama Cloudの応答 |
  |---|---|
  | 素の `tools` | HTTP 200 / ツール呼び出し成功 |
  | `defer_loading: true` 付き | HTTP 200 / ツール呼び出し成功 |
  | `tool_search_tool_regex_20251119` + `defer_loading` | HTTP 200 / ツール呼び出し成功 |
  | 履歴に `tool_reference` ブロック | HTTP 200 / ツール呼び出し成功 |

  Ollamaは `defer_loading` / `tool_search_tool_*` / `tool_reference` を黙って無視した上で、ツール呼び出し自体は正しく行います。ただし**`defer_loading` が無視される＝フォールバック中は全ツール定義がモデルに渡る**ということでもあるので、下記「フォールバック先モデルの選定」を参照してください。
- **Remote Control は無効になります**（Claude Code v2.1.196以降、`api.anthropic.com` 以外を指した場合）。これは仕様上回避できません。

## フォールバック先モデルの選定

Claude Codeはエージェントであり、ツール呼び出しができないモデルでは実質的に機能しません。`model_mapping` の `target` には**必ずtool calling対応モデルを指定してください**。

確認方法（Ollama Cloudへ直接投げて `tool_use` が返るかを見ます）:

```bash
curl -s https://ollama.com/v1/messages \
  -H "authorization: Bearer $OLLAMA_API_KEY" \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"確認したいモデル名","max_tokens":128,
       "tools":[{"name":"get_weather","description":"Get the weather",
                 "input_schema":{"type":"object","properties":{"location":{"type":"string"}},
                                 "required":["location"]}}],
       "messages":[{"role":"user","content":"What is the weather in Tokyo? Use the tool."}]}' \
  | python3 -c "import json,sys; print([b.get('name') for b in json.load(sys.stdin)['content'] if b.get('type')=='tool_use'])"
```

`['get_weather']` が返れば対応しています。空リストなら、そのモデルはフォールバック先に使えません。

あわせて**コンテキスト長**にも注意してください。MCPサーバーを多数使っていると、tool searchが有効でもフォールバック中は（Ollamaが `defer_loading` を無視するため）全ツール定義がモデルへ渡ります。Ollamaの公式ドキュメントはClaude Code用途に64K以上を推奨しており、`config.example.yaml` のコメントにも各モデルのコンテキスト長を注記しています。

> 既知のリスク: Ollama Cloudは非常に大きなリクエスト（18KB超のシステムプロンプト、20個超のツール）で500エラーを返すという報告があります（[ollama/ollama#13949](https://github.com/ollama/ollama/issues/13949) 周辺）。MCPを多用する構成でフォールバックが失敗する場合は、`monitor` の「直近エラー」欄を確認してください。

## 5. 動作確認

```bash
# WSL内から
curl -s http://127.0.0.1:8787/_spillway/status | python3 -m json.tool

# Windows側から到達できるか（WSL内から実行可能）
/mnt/c/Windows/system32/curl.exe -s -o /dev/null -w "HTTP %{http_code}\n" \
  http://127.0.0.1:8787/_spillway/status

# 実際にAnthropicへ転送されているか（無認証なので401が返れば正常。quota消費なし）
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8787/v1/messages \
  -H 'content-type: application/json' \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"x"}]}'
```

TUIでの監視:

```bash
claude-spillway monitor
```

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `address already in use` で起動失敗 | 手動起動したインスタンスが残っています。`ss -tlnp \| grep 8787` で特定して停止してください |
| Claude Codeが全く接続できない | プロキシが停止しています。`systemctl --user status claude-spillway` を確認。`ANTHROPIC_BASE_URL` を設定している間、プロキシはClaude Codeの生命線です |
| フェイルオーバー時のみ認証エラー | `OLLAMA_API_KEY` がサービスに渡っていません。`systemctl --user show claude-spillway -p Environment` と `EnvironmentFile` のパスを確認 |
| Windows側からだけ繋がらない | `%USERPROFILE%\.wslconfig` で `localhostForwarding=false` にしていないか確認。それでも駄目なら `wsl --shutdown` で再起動 |
| `monitor` が「待機中」のまま | 正常です。このプロキシはAnthropicの認証情報を持たず、実リクエストのレスポンスヘッダーからのみquotaを観測するため、Claude Codeから1回リクエストが通るまで数値は出ません |

## 既知の制約

- **`wsl --shutdown` を実行するとプロキシも停止します。** 次にWSLが起動した時点でsystemdが再起動します。
- **常駐プロセスがあるとWSLのVMは終了しなくなります。** プロキシ自体は40MB程度なので実害はありませんが、メモリを完全に解放したい場合は `systemctl --user stop claude-spillway` してから `wsl --shutdown` してください。
- ユーザーサービスは既定では最初のログインセッション開始時に起動します。ログインなしでディストロ起動時から常駐させたい場合は `sudo loginctl enable-linger $USER` を実行してください（VSCode Remote-WSLで使う分には不要です）。
