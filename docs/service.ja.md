# サービスとしての常駐運用

claude-spillway を systemd ユーザーサービスとして常駐させ、ログイン時に自動起動させる手順です。

[English version](service.md)

Windows + WSL2 環境固有の事情（Windows側VSCodeからの接続、ディストロの起動タイミングなど）は [wsl-windows.ja.md](wsl-windows.ja.md) にまとめています。

## なぜサービス化するか

`ANTHROPIC_BASE_URL` をこのプロキシに向けている間、**プロキシが停止するとすべてのClaude Codeセッションが接続不能になります**。手動起動やスタートアップスクリプトでは、落ちたことに気づけず、復帰もしません。systemdユーザーサービスなら以下が手に入ります。

- `Restart=always` による自動復帰
- ログイン時の自動起動
- `journalctl` によるログ
- `EnvironmentFile` によるAPIキーの分離（設定ファイルに直書きしない）

## 導入

```bash
# 1. 本体を導入
uv tool install claude-spillway

# 2. サービスとして登録
git clone https://github.com/akivajp/claude-spillway.git
cd claude-spillway
./scripts/install-service.sh
```

スクリプトは以下を行います。

1. `~/.config/claude-spillway/` を作成し、設定ファイルの雛形を配置
2. Ollama Cloud APIキーを尋ね、`~/.config/claude-spillway/env` に `chmod 600` で保存
3. unitファイルを `~/.config/systemd/user/` へ配置（`ExecStart` は実行環境の絶対パスに解決）
4. `systemctl --user enable --now` で起動し、応答を確認

**冪等です。** 何度実行しても、既存の `config.yaml` と `env` は上書きしません。本体を更新した後に再実行して unit を貼り直す、といった使い方ができます。

APIキーを対話で入力したくない場合は、環境変数に入れておけばそちらが使われます。

```bash
OLLAMA_API_KEY=your-key ./scripts/install-service.sh
```

### 導入後にやること

Claude Code にプロキシの場所を教えます（`~/.claude/settings.json`）。

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8787",
    "API_FORCE_IDLE_TIMEOUT": "0"
  }
}
```

`API_FORCE_IDLE_TIMEOUT=0` はストリーミングの5分アイドルタイムアウトを解除するもので、フォールバック中のOllamaモデルはチャンク間隔が空きやすいため推奨です。詳細は [README.ja.md](../README.ja.md) を参照してください。

## 日常の操作

```bash
systemctl --user status claude-spillway      # 状態確認
systemctl --user restart claude-spillway     # 再起動
systemctl --user stop claude-spillway        # 停止
journalctl --user -u claude-spillway -f      # ログ追跡
claude-spillway monitor                      # TUIで残量を監視
claude-spillway dashboard                    # ブラウザで残量を監視
```

## 更新

```bash
uv tool upgrade claude-spillway
systemctl --user restart claude-spillway
```

`ExecStart` は実行ファイルの絶対パスを指しているため、通常はunitの貼り直しは不要です。インストール先が変わった場合のみ `./scripts/install-service.sh` を再実行してください。

## アンインストール

```bash
./scripts/install-service.sh --uninstall
```

サービスを停止・無効化し、unitファイルを削除します。**設定ファイルとAPIキーは残します**（再導入時に入力し直す手間を避けるため）。完全に消す場合は以下を実行してください。

```bash
rm -rf ~/.config/claude-spillway
```

## 設定の変更

設定ファイルは `~/.config/claude-spillway/config.yaml` です。`-c` を省略した場合の探索先そのものなので、編集後は再起動するだけで反映されます。

```bash
$EDITOR ~/.config/claude-spillway/config.yaml
systemctl --user restart claude-spillway
```

全項目は [config.example.yaml](../config.example.yaml) を参照してください。

### 複数マシンでの共有

`config.yaml` に秘密情報は含まれません（APIキーは別ファイルの `env` に分離されています）。そのため dotfiles リポジトリに置いてシンボリックリンクで配置できます。

```bash
ln -s ~/dotfiles/claude-spillway/config.yaml ~/.config/claude-spillway/config.yaml
```

インストーラは既存の `config.yaml` を（シンボリックリンクであっても）上書きしないため、**リンクを先に張ってから**実行するのが簡単です。

設定ファイルは本体の更新に追従しません。`uv tool upgrade` の後は、雛形と差分を取って新しく増えた項目を確認してください。

```bash
diff <(sed -n '/^listen:/,$p' ~/.config/claude-spillway/config.yaml) \
     <(sed -n '/^listen:/,$p' config.example.yaml)
```

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| `systemctl が見つかりません` | systemdが動いていません。WSLの場合は `/etc/wsl.conf` に `[boot] systemd=true` を書き、`wsl --shutdown` してください |
| `claude-spillway が見つかりません` | 先に `uv tool install claude-spillway` を実行してください |
| 起動直後に `address already in use` | 手動起動したインスタンスが残っています。`ss -tlnp \| grep 8787` で特定して停止してください |
| フェイルオーバー時だけ認証エラー | `OLLAMA_API_KEY` がサービスに渡っていません。`systemctl --user show claude-spillway -p Environment` と `~/.config/claude-spillway/env` を確認してください |
| `monitor` が「待機中」のまま | 正常です。このプロキシはAnthropicの認証情報を持たず、Claude Codeが送ってきたものを借りる設計のため、実リクエストが1回通るまで数値が出ません |
| ログアウトすると停止する | 既定ではユーザーサービスはログインセッションに紐づきます。ログイン前から常駐させたい場合は `sudo loginctl enable-linger $USER` を実行してください |

## unitファイルについて

配置されるunitの実体は [packaging/systemd/claude-spillway.service](../packaging/systemd/claude-spillway.service) です。手で編集したい場合はこれを参考にしてください。ポイントは以下の2点です。

- **`Restart=always` / `RestartSec=5`** — プロキシの停止はClaude Code全体の停止を意味するため、必須級です
- **`EnvironmentFile`** — APIキーを設定ファイルから分離します。`config.yaml` 側は `api_key: ${OLLAMA_API_KEY}` のままにしておけば、プロセス環境変数から展開されます
