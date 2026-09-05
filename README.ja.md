# claude-spillway

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
- フォールバック中は、バックグラウンドで定期的にAnthropicへ軽量なプローブリクエストを送って回復を確認します。残量比率が`recovery_threshold_pct`(デフォルト20%。fallback閾値より意図的に高く設定し、フラッピングを防止)を上回ったら、Anthropicへ切り戻します。

### なぜ`/v1/messages`だけをフェイルオーバー対象にしているか

フェイルオーバーの対象は実際の推論呼び出し(`POST /v1/messages`)のみです。`/v1/messages/count_tokens`やモデル一覧取得等の補助エンドポイントは、常にAnthropicへ転送され、Ollamaには一切送られません。これは意図的な設計です。Ollama Cloudの Anthropic互換シムは、未対応のエンドポイントへのリクエストを受けるとハングし、サーバーが再起動してしまうという既知の問題があり([ollama/ollama#13949](https://github.com/ollama/ollama/issues/13949))、トークン数が取れない程度では済まない影響が出るためです。

### Ollama Cloud側のquota可視性について

2026年9月時点で、Ollama Cloudにはアカウントの残りquotaを確認する公式APIが**存在しません**([ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663), [#16448](https://github.com/ollama/ollama/issues/16448))。そのため、claude-spillwayはAnthropicのように正確な「Ollama残量%」を表示することはできません。代わりに、ステータスエンドポイントとTUIでは、このプロキシを経由したトラフィックについての自己計測値(中継リクエスト数、失敗数、直近のステータスコード)を報告します。

## インストール

Python 3.11以降と[uv](https://docs.astral.sh/uv/)が必要です。

```bash
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
uv sync
```

## クイックスタート

1. 設定ファイルの例をコピーし、Ollama CloudのAPIキーを設定します
   (<https://ollama.com/settings/keys> から発行できます):

   ```bash
   cp config.example.yaml config.yaml
   export OLLAMA_API_KEY=your-ollama-cloud-api-key
   ```

2. プロキシを起動します:

   ```bash
   uv run claude-spillway serve -c config.yaml
   ```

3. Claude Codeをこのプロキシに向けて、いつも通り起動します:

   ```bash
   export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
   claude
   ```

4. (任意) 別のターミナルでquota状況をリアルタイム監視できます:

   ```bash
   uv run claude-spillway monitor
   ```

   claude-spillwayはAnthropicの認証情報を自前で保持しないため、Claude Codeが
   実際に送ってきたリクエストのレスポンスヘッダーを読んで初めてquotaを知る
   設計になっています。そのため、少なくとも1回リクエストが通るまで`monitor`
   は「待機中」と表示し続けます(自発的にAnthropicへ問い合わせることはできません)。

実際の資格情報を使わずに一連の流れを試したい場合は、同梱のダミーサーバーが使えます。[`scripts/manual_smoketest/`](scripts/manual_smoketest/)を参照してください。

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
| `quota.probe_interval_seconds` | `60.0` | フォールバック中にAnthropicへ回復確認する間隔 |
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

## ライセンス

[MIT](LICENSE)
