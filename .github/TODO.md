# TODO / 覚書

## Ollama Cloudのquotaリセット時刻

**状態**: 解決（2026-10-14）。ollama.com の設定ページ（Usageタブ）をセッションCookieで読み、使用率と実測リセット時刻を取得するようにした。

2026-10-14 に `/api/usage` の形式が変わり、使用率（`limits`）を返さずリクエスト数のみを返すようになった。設定ページはHTMLに `aria-label="Session usage N% used"` と `data-time="<ISO 8601 UTC>"`（正確なリセット時刻）を埋め込んでおり、`__Secure-session` Cookie だけで取得できる（JSON APIは無い。Cookieはログインから約90日で失効し、アクセスで延長されない）。下記で「不採用」としたスクレイピングを、取得間隔を空ける（`ollama.usage_page_interval_seconds`、既定120秒）ことで採用した形。予測値（`OllamaResetEstimator`）は実測値が無い場合の代替として残している。以下は解決前の記録。

### 背景

Ollama Cloudには、ドキュメント化されたquota APIが存在しない（[ollama/ollama#15663](https://github.com/ollama/ollama/issues/15663) → [#12532](https://github.com/ollama/ollama/issues/12532) へ重複クローズ、公式応答なし）。claude-spillwayは非公開の `GET /api/usage` を利用し、セッション窓・週次窓の使用率（0〜1の比率）とモデル別リクエスト数を取得しているが、**レスポンスにリセット時刻は含まれない**。

そのため現在は、利用率の上昇を検出した時刻を「新しい窓の始点」とみなし、「始点＋窓の最大長」を**リセット時刻の上限値**として推定している（実装: [recovery.py](src/claude_spillway/recovery.py) の `OllamaResetEstimator`、窓長定数は [quota.py](src/claude_spillway/quota.py) の `OLLAMA_SESSION_WINDOW_SECONDS` / `OLLAMA_WEEKLY_WINDOW_SECONDS`）。TUIでは波ダッシュ（`~`）付きで表示し、実測値と区別している。

この予測は**表示専用**であり、ルーティング判断には使われない。`burn_rate_balance` ポリシーは、リセット時刻が不明な窓を「始まったばかり（最も余裕がある）」として扱うため、予測値に依存しない。

### 調査済みの選択肢

| 方法 | 結果 |
|---|---|
| `/v1/messages` のレスポンスヘッダー | quota関連のヘッダーは一切付かない（全件確認済み） |
| `ollama` CLIバイナリの解析 | 使用量系のエンドポイントは `/api/usage` のみ。リセット時刻の痕跡なし |
| ollama.comダッシュボードのJS解析 | ダッシュボードは認証後で解析不能（サインインページにリダイレクト） |
| issue #15663 | 公式応答なし。#12532へ重複クローズ |
| ダッシュボードのDOM解析（スクレイピング） | ユーザースクリプト[ollama-usage-breakdown](https://github.com/srnoob2570/ollama-usage-breakdown)が存在。バー幅と「Resets in 2 hours」形式の相対表記を読む方式で、フォーマット変更に脆弱。常時スクレイピングはボット判定のリスクがあるため**不採用** |
| [ollama-usage](https://git.sr.ht/~hrbrmstr/ollama-usage)（sr.ht） | ボットチェックにより内容確認不能。`cookie` ディレクトリを持つ＝セッションCookieによるスクレイピングとみられる。同様の理由で**不採用** |

### 将来の対応（公式APIが公開された場合）

1. [backends.py](src/claude_spillway/backends.py) の `fetch_ollama_usage` が参照するレスポンスへ、リセット時刻フィールドが追加されているか確認する
2. 追加されていれば [quota.py](src/claude_spillway/quota.py) の `parse_ollama_usage` で読み込み、`OllamaSnapshot` の `estimated_*_reset` を実測値に置き換える（フィールド名は `estimated_*` から `reset_*` へ変更し、Anthropic側と同じ扱いにする）
3. `OllamaResetEstimator`（[recovery.py](src/claude_spillway/recovery.py)）と `OLLAMA_*_WINDOW_SECONDS`（[quota.py](src/claude_spillway/quota.py)）を削除
4. [monitor.py](src/claude_spillway/monitor.py) の `_fmt_estimated_reset` を `_fmt_reset` に統合し、波ダッシュを廃止
5. README（英・日）の「Ollama reports no reset times」の記述を更新

### 別の改善の可能性

- 利用率の**減少**はリセット（または枠の更新）を示す。減少を検出した時刻を「窓の終点」として記録すれば、窓長の実測値が蓄積され、予測精度が上がる可能性がある（未実装）
- `usage` の他に `activity.period.starting_at` / `ending_at` というフィールドがあるが、これは集計期間（過去4週間）を示すものであり、quota窓とは別物。混同しないこと