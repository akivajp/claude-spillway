# claude-spillway

[![PyPI](https://img.shields.io/pypi/v/claude-spillway)](https://pypi.org/project/claude-spillway/)
[![Python](https://img.shields.io/pypi/pyversions/claude-spillway)](https://pypi.org/project/claude-spillway/)

[English README is here](README.md)

[Claude Code](https://claude.com/claude-code) 用の、quota連動フェイルオーバープロキシです。

`claude-spillway` はClaude CodeとAnthropic APIの間に立つプロキシです。通常はAnthropicへそのまま転送しますが、すべてのレスポンスに付与されるレート制限ヘッダーを監視し、あなたのquota(Claude Pro/Max/Teamサブスクリプションなら5時間窓・週次窓の利用率、APIキー課金なら従来のリクエスト数/トークン数の残量)が逼迫すると、自動的に`POST /v1/messages`のトラフィックを[Ollama Cloud](https://ollama.com)へ「溢れさせ(spill over)」ます。quotaが回復すれば自動的にAnthropicへ戻ります。

これは非常に具体的な悩みを解決するために作りました: Claudeサブスクリプションと Ollama Cloud の両方に課金しているなら、セッション途中でAnthropicのレート制限に完全にブロックされてしまうより、(多くの場合より安価で余裕のある) Ollamaのquotaを自動的に消費してほしい、というものです。

## 仕組み

```
Claude Code --ANTHROPIC_BASE_URL--> claude-spillway --> Anthropic API
                                          |
                                          '--(quota逼迫)--> Ollama Cloud
                                                             (Anthropic互換の
                                                              /v1/messages)
```

- Anthropicからのすべてのレスポンスには、レート制限ヘッダー(サブスクリプションプランなら`anthropic-ratelimit-unified-5h-utilization`/`anthropic-ratelimit-unified-7d-utilization`、APIキー課金なら`anthropic-ratelimit-{requests,tokens}-{limit,remaining}`)が付与されています。claude-spillwayはこれを毎リクエストで読み取るだけなので、利用状況を知るための追加のAPI呼び出しは不要です。
- 既知の指標のうち最も逼迫している残量比率が`fallback_threshold_pct`(デフォルト10%)を下回ると、以降の`POST /v1/messages`は`model_mapping`設定に従ってモデル名を書き換えた上でOllama Cloudへルーティングされます。
- claude-spillway自体はAnthropicの認証情報を保持する必要がありません。Claude Codeが送ってくる`Authorization`/`x-api-key`ヘッダーをそのまま横流しするだけです。Ollama Cloud側だけ、設定ファイルにAPIキーが必要です(Ollama側のAnthropic互換エンドポイントは`x-api-key`を受け付けず`Authorization: Bearer`のみ有効なため。[ollama/ollama#16922](https://github.com/ollama/ollama/issues/16922)参照)。
- バックグラウンドのプローブが、OAuthの使用量エンドポイント(Claude Code自身が`/usage`で参照しているもの)からquotaを読み取ります。**quotaを一切消費せず**、各ウィンドウのリセット時刻も取得できます。残量比率が`recovery_threshold_pct`(デフォルト20%。fallback閾値より意図的に高く設定し、フラッピングを防止)を上回ったら、Anthropicへ切り戻します。このエンドポイントはOAuth専用かつ公開APIではないため、利用できない場合は中継したレスポンスのヘッダーへ、さらにトラフィックが流れないフォールバック中に限り、わずかにquotaを消費する軽量な`/v1/messages`リクエストへとフォールバックします。

### なぜ`/v1/messages`だけをフェイルオーバー対象にしているか

フェイルオーバーの対象は実際の推論呼び出し(`POST /v1/messages`)のみです。`/v1/messages/count_tokens`やモデル一覧取得等の補助エンドポイントは、常にAnthropicへ転送され、Ollamaには一切送られません。これは意図的な設計です。Ollama Cloudの Anthropic互換シムは、未対応のエンドポイントへのリクエストを受けるとハングし、サーバーが再起動してしまうという既知の問題があり([ollama/ollama#13949](https://github.com/ollama/ollama/issues/13949))、トークン数が取れない程度では済まない影響が出るためです。

### Ollama Cloud側のquota可視性について

Ollama Cloudには、ドキュメント化された公式のquota APIが**依然として存在せず**([ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663), [#16448](https://github.com/ollama/ollama/issues/16448))、推論レスポンスにもレート制限ヘッダーが一切付きません。ただし、Ollama自身のダッシュボードが参照している非公開の `GET /api/usage` は推論と同じAPIキーで叩け、その読み取り自体はリクエスト数に計上されません。claude-spillwayはここからセッション窓・週次窓の使用率とモデル別リクエスト数を取得しています。

注意点が2つあります。非公開エンドポイントであるため、形が変わった場合はエラーではなく「データなし」に縮退します。またAnthropicと異なり、**Ollamaはリセット時刻を返しません**。そのため、Ollama側の窓がいつ切り替わるかは本ツールでは表示できません。

### Ollamaのリセット時刻について(予測表示)

リセット時刻を取得するAPIは存在しないため(調査の詳細は[.github/TODO.md](.github/TODO.md))、利用率の動きからの**予測表示**を行います。利用率は窓がリセットされない限り下がらないため、上昇を検出した時刻を「新しい窓の始点」とみなし、「始点＋窓の最大長(セッション窓は5時間、週次窓は7日)」を**リセットが確実に済んでいる上限時刻**として表示します。真の値はこれより早い可能性があるため、TUIでは`~`を付けて実測値と区別します。

この予測は表示専用であり、ルーティング判断には使われません。`burn_rate_balance`ポリシーは、リセット時刻が不明な窓を「始まったばかり」として扱うため、予測値に依存しません。

ステータスエンドポイントとTUIでは、これに加えてこのプロキシを経由したトラフィックのみの自己計測値(中継リクエスト数、失敗数、直近のステータスコード)も報告します。

### バックエンドの選択

両者のquotaが見えるようになったため、どちらも逼迫していない状況での選択を `routing.policy` で指定できます。

- `anthropic_first`(デフォルト) — Anthropicが逼迫するまで使い続け、逼迫したらフェイルオーバーする。従来の挙動
- `weekly_balance` — 短い窓が両方とも余裕のある間(`balance_session_floor_pct`)は、**週次窓**の残量が多い方を優先する。相手側が `balance_margin_pct` 以上優っている場合のみ切り替えるため、拮抗した2者間で振動しない
- `burn_rate_balance` — 窓ごとに「残量 ÷ (リセットまでの時間 ÷ 窓の全長)」を求め、バックエンドごとに最も逼迫した窓を採用し、その値が良い方を優先する。リセット時刻が不明な場合(Ollamaは常にこれに該当)は「窓が始まったばかり」と仮定して最も甘く見積もるため、不明であることを根拠に焦りは生まない。`anthropic_priority_weight`(デフォルト1.1)により比較はClaudeサブスクリプション側に傾く。拮抗していればAnthropicが勝ち、約9%劣るまでトラフィックはOllamaへ移らない。ある側がリセットまでに使い切りそうなペースでquotaを消費している場合の安全弁として使う

ポリシーに関係なく、常時2つのガードが働きます。

- **Ollama枯渇ガード** — Ollama側の残量が `ollama_min_remaining_pct` を下回っている場合はフェイルオーバー先にしない(袋小路を別の袋小路に取り替えるだけのため)
- **逆フェイルオーバー** — Ollama Cloudは特定モデルへのアクセス集中時に遅延・失敗することがある。`ollama_failure_threshold` 回連続で失敗したらAnthropicへ戻す。ただしAnthropicの5時間窓に `reverse_failover_min_5h_pct` 以上の残量がある場合に限り、戻した後は `reverse_failover_cooldown_seconds` の間Ollamaを使わない

さらに上位のハードガードとして、Anthropicの残量が `quota.fallback_threshold_pct` を下回った場合は、ポリシーに関係なくフェイルオーバーします。

## インストール

Python 3.11以降と[uv](https://docs.astral.sh/uv/)が必要です。

```bash
uv tool install claude-spillway
```

インストールせずに実行することもできます:

```bash
uvx --from claude-spillway claude-spillway serve
```

開発する場合はリポジトリをクローンしてください:

```bash
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
uv sync
```

## クイックスタート

1. `serve` が既定で読む場所に設定ファイルの例を配置し、Ollama CloudのAPIキーを設定します
   (<https://ollama.com/settings/keys> から発行できます):

   ```bash
   mkdir -p ~/.config/claude-spillway
   curl -o ~/.config/claude-spillway/config.yaml \
     https://raw.githubusercontent.com/akivajp/claude-spillway/main/config.example.yaml
   export OLLAMA_API_KEY=your-ollama-cloud-api-key
   ```

2. プロキシを起動します:

   ```bash
   claude-spillway serve
   ```

3. Claude Codeをこのプロキシに向けて、いつも通り起動します:

   ```bash
   export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
   claude
   ```

4. (任意) 別のターミナルでquota状況をリアルタイム監視できます:

   ```bash
   claude-spillway monitor
   ```

   claude-spillwayはAnthropicの認証情報を自前で保持せず、Claude Codeが送って
   きたものを借りる設計です。そのため、少なくとも1回リクエストが通るまで
   `monitor`は「待機中」と表示し続けます。それ以降は自発的にポーリングを行い、
   使用量エンドポイントの読み取りはquotaを消費しないため、操作していない間も
   表示は最新に保たれます。

実際の資格情報を使わずに一連の流れを試したい場合は、同梱のダミーサーバーが使えます。[`scripts/manual_smoketest/`](scripts/manual_smoketest/)を参照してください。

毎回手で起動する代わりに、systemdユーザーサービスとして常駐させることもできます。スクリプト1つで完結します。

```bash
./scripts/install-service.sh
```

何が設定されるか、更新・削除の手順は [docs/service.ja.md](docs/service.ja.md) を参照してください。

Windows + WSL2環境で使う場合は、[docs/wsl-windows.ja.md](docs/wsl-windows.ja.md) にWindows側・WSL側両方のClaude Code拡張への設定方法をまとめています。

## 設定

全項目は[`config.example.yaml`](config.example.yaml)を参照してください。

`-c` を省略した場合、次の順で設定ファイルを探索します:

1. 環境変数 `CLAUDE_SPILLWAY_CONFIG` が指すパス
2. `~/.config/claude-spillway/config.yaml`(`$XDG_CONFIG_HOME` を尊重。Windowsでは `%APPDATA%\claude-spillway\config.yaml`)

どちらにも無い場合は組み込みのデフォルト値で起動します。主な項目:

| 項目 | デフォルト | 説明 |
|---|---|---|
| `listen.host` / `listen.port` | `127.0.0.1` / `8787` | claude-spillwayの待受アドレス |
| `anthropic.base_url` | `https://api.anthropic.com` | Anthropic APIのエンドポイント |
| `ollama.base_url` | `https://ollama.com` | Ollama Cloudのエンドポイント |
| `ollama.api_key` | — | Ollama CloudのAPIキー(`${ENV_VAR}`形式で環境変数展開可) |
| `quota.fallback_threshold_pct` | `10.0` | この残量%を下回るとフェイルオーバーする |
| `quota.recovery_threshold_pct` | `20.0` | この残量%まで回復すると切り戻す |
| `quota.probe_interval_seconds` | `60.0` | バックグラウンドプローブがquotaを再取得する間隔 |
| `quota.use_usage_endpoint` | `true` | OAuth使用量エンドポイントからquotaを読む(quota消費なし) |
| `routing.policy` | `anthropic_first` | `anthropic_first` / `weekly_balance` / `burn_rate_balance` |
| `routing.anthropic_priority_weight` | `1.1` | burn_rate_balance: Anthropic側の優先度 |
| `routing.ollama_min_remaining_pct` | `5.0` | Ollama側がこの残量%を下回ったらフェイルオーバーしない |
| `routing.ollama_failure_threshold` | `5` | この回数連続で失敗したらAnthropicへ戻す |
| `model_mapping.rules` / `model_mapping.default` | — | Anthropicのモデル名 -> Ollama側のモデル名 |

`claude-spillway serve`のCLIオプション(`--host`, `--port`, `--fallback-threshold-pct`, `--recovery-threshold-pct`, `--log-level`)は設定ファイルより優先されます。詳細は`claude-spillway --help` / `claude-spillway serve --help` / `claude-spillway monitor --help`を実行してください。

### 表示言語

CLIのヘルプや`monitor`のTUIは、デフォルトでは英語で表示され、環境のロケール(`LC_ALL`, `LC_MESSAGES`, `LANG`, `LANGUAGE`のいずれかが`ja`始まり)が日本語の場合のみ日本語で表示されます。`CLAUDE_SPILLWAY_LANG`で明示的に上書きできます:

```bash
CLAUDE_SPILLWAY_LANG=en claude-spillway monitor  # 英語に固定
CLAUDE_SPILLWAY_LANG=ja claude-spillway monitor  # 日本語に固定
```

なお、`logging`経由のログメッセージは、grepや検索のしやすさを優先して常に英語で出力されます。

## 開発

```bash
uv run pytest       # ユニットテスト・結合テスト(ネットワーク接続不要)
uv run ruff check .  # lint
```

テストはモード切替・ヒステリシスのロジックも含めてリクエスト/レスポンスの全経路を`httpx.MockTransport`で検証しているため、オフラインで実行でき、実際のAnthropic/Ollamaのquotaも一切消費しません。

## ステータス

まだ初期段階のプロジェクトです。個人利用のために作り、他の人にも役立つかもしれないと考えて公開しています。AnthropicおよびOllamaとは無関係の非公式プロジェクトです。

## 支援

claude-spillwayがレート制限による中断を防ぐのに役立ったら、
[コーヒーをおごって](https://buymeacoffee.com/akivajp)いただけると嬉しいです。

[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-ffdd00?style=flat-square&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/akivajp)

## ライセンス

[MIT](LICENSE)
