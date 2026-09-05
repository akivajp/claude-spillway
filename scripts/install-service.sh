#!/usr/bin/env bash
# claude-spillway を systemd ユーザーサービスとして常駐させる導入スクリプト。
# Install claude-spillway as a systemd user service.
#
# 冪等: 何度実行しても既存の設定ファイルとAPIキーは上書きしない。
# Idempotent: re-running never overwrites an existing config file or API key.
#
# 使い方 / Usage:
#   ./scripts/install-service.sh              # 導入 / install
#   ./scripts/install-service.sh --uninstall  # 削除 / remove
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claude-spillway"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_NAME="claude-spillway.service"
CONFIG_EXAMPLE_URL="https://raw.githubusercontent.com/akivajp/claude-spillway/main/config.example.yaml"

info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m警告 / warning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31mエラー / error:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 前提条件の確認 / preflight -------------------------------------------

require_systemd() {
    # systemd が無い環境(WSLでsystemd=trueを未設定など)を、無言で失敗させない。
    # Fail loudly on a system without systemd (e.g. WSL without systemd=true),
    # rather than leaving a unit file that nothing will ever run.
    command -v systemctl >/dev/null 2>&1 || die \
        "systemctl が見つかりません。WSLの場合は /etc/wsl.conf に [boot] systemd=true を書き、wsl --shutdown してください。
systemctl not found. On WSL, put [boot] systemd=true in /etc/wsl.conf and run wsl --shutdown."
    systemctl --user show-environment >/dev/null 2>&1 || die \
        "systemd のユーザーセッションが動作していません。
The systemd user session is not running."
}

find_executable() {
    # uv tool install は ~/.local/bin に置く。PATHが通っていない環境でも動くよう
    # 絶対パスで解決し、見つからなければ導入方法を案内する。
    # uv tool install puts it in ~/.local/bin. Resolve an absolute path so the
    # unit works regardless of PATH, and explain how to install it if missing.
    local candidate
    for candidate in "$HOME/.local/bin/claude-spillway" "$(command -v claude-spillway 2>/dev/null || true)"; do
        if [ -n "$candidate" ] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    die "claude-spillway が見つかりません。先に 'uv tool install claude-spillway' を実行してください。
claude-spillway not found. Run 'uv tool install claude-spillway' first."
}

# --- 導入 / install --------------------------------------------------------

install_config() {
    mkdir -p "$CONFIG_DIR"

    if [ -f "$CONFIG_DIR/config.yaml" ]; then
        info "設定ファイルは既に存在します(変更しません) / config file already exists, leaving it alone"
    else
        info "設定ファイルを配置します / installing the example config"
        local repo_example
        repo_example="$(dirname "$(dirname "$(readlink -f "$0")")")/config.example.yaml"
        if [ -f "$repo_example" ]; then
            cp "$repo_example" "$CONFIG_DIR/config.yaml"
        else
            # リポジトリ外から実行された場合(curl | bash 等)はダウンロードする。
            # Fetch it when run outside a checkout (e.g. piped from curl).
            command -v curl >/dev/null 2>&1 || die "curl が必要です / curl is required"
            curl -fsSL "$CONFIG_EXAMPLE_URL" -o "$CONFIG_DIR/config.yaml"
        fi
    fi

    if [ -f "$CONFIG_DIR/env" ]; then
        info "APIキーは設定済みです(変更しません) / API key already set, leaving it alone"
        return
    fi

    # 環境変数から拾えるならそれを使い、無ければ対話で尋ねる。設定ファイルには
    # 書かず env ファイルに分離し、パーミッションを絞る。
    # Prefer an exported key, else ask. It goes in a separate env file with
    # tight permissions, never into the config file itself.
    local key="${OLLAMA_API_KEY:-}"
    if [ -z "$key" ] && [ -t 0 ]; then
        printf 'Ollama Cloud APIキー (https://ollama.com/settings/keys、空欄でスキップ): '
        read -r key
    fi
    if [ -z "$key" ]; then
        warn "APIキーが未設定です。フェイルオーバー時のリクエストは失敗します。
後で $CONFIG_DIR/env に OLLAMA_API_KEY=... を書いてください。
No API key set; requests will fail once it fails over. Add OLLAMA_API_KEY=... to that file later."
        return
    fi
    printf 'OLLAMA_API_KEY=%s\n' "$key" > "$CONFIG_DIR/env"
    chmod 600 "$CONFIG_DIR/env"
    info "APIキーを $CONFIG_DIR/env に保存しました (chmod 600)"
}

install_unit() {
    local exe unit_source
    exe="$(find_executable)"
    mkdir -p "$UNIT_DIR"

    unit_source="$(dirname "$(dirname "$(readlink -f "$0")")")/packaging/systemd/$UNIT_NAME"
    [ -f "$unit_source" ] || die "unitファイルが見つかりません: $unit_source"

    info "unitファイルを配置します / installing the unit file"
    # ExecStart だけは実行環境で解決した絶対パスに置き換える。
    # Substitute the resolved absolute path into ExecStart.
    sed "s|^ExecStart=.*|ExecStart=$exe serve|" "$unit_source" > "$UNIT_DIR/$UNIT_NAME"

    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
}

verify() {
    info "起動を確認します / verifying"
    local port
    port="$(sed -n 's/^ *port: *\([0-9]\+\).*/\1/p' "$CONFIG_DIR/config.yaml" 2>/dev/null | head -1)"
    port="${port:-8787}"

    local i
    for i in $(seq 1 40); do
        if curl -fsS -o /dev/null "http://127.0.0.1:$port/_spillway/status" 2>/dev/null; then
            info "起動しました / running: http://127.0.0.1:$port"
            printf '\n次の手順 / next steps:\n'
            printf '  1. Claude Code にプロキシを教える (~/.claude/settings.json):\n'
            printf '     {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%s"}}\n' "$port"
            printf '  2. 状態を見る / watch it:  claude-spillway monitor\n'
            printf '  3. ログを見る / logs:      journalctl --user -u claude-spillway -f\n'
            return 0
        fi
        sleep 0.25
    done

    warn "応答がありません。ログを確認してください / no response yet; check the logs:
  journalctl --user -u claude-spillway -n 30"
}

uninstall() {
    require_systemd
    info "サービスを停止・無効化します / stopping and disabling the service"
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT_NAME"
    systemctl --user daemon-reload
    # 設定とAPIキーは消さない。再導入時に書き直す手間を避けるため。
    # The config and API key are kept, so a re-install needs no retyping.
    info "削除しました。設定は $CONFIG_DIR に残しています。
Removed. The config is kept at that path; delete it by hand if you want it gone."
}

main() {
    case "${1:-}" in
        --uninstall|-u)
            uninstall
            ;;
        --help|-h)
            # 冒頭のコメントブロックをそのままヘルプとして出す。行番号ではなく
            # 「コメントが途切れるまで」で切るので、編集で位置がずれても壊れない。
            # Print the leading comment block as help. Cutting at the first
            # non-comment line survives edits that shift the line numbers.
            sed -n '2,${/^#/!q; s/^# \?//; p;}' "$0"
            ;;
        "")
            require_systemd
            install_config
            install_unit
            verify
            ;;
        *)
            die "不明な引数: $1 (--help を参照 / see --help)"
            ;;
    esac
}

main "$@"
