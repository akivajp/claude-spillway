"""Capture the dashboard of a running proxy for the README, one image per language.

Run against a proxy that has seen real traffic, so both cards hold numbers:

    uv run --extra login python scripts/dashboard_screenshot.py

稼働中プロキシのダッシュボードを、README用に言語ごとに撮影するスクリプト。
両方のカードに数値が出ている(実トラフィックが流れた)プロキシに対して実行する。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

#: 言語コードと、ブラウザに名乗らせるロケール。
#: Language code -> the locale the browser announces.
_LOCALES = {"en": "en-US", "ja": "ja-JP"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture docs/images/dashboard-<lang>.png from a running proxy.")
    parser.add_argument("--url", default="http://127.0.0.1:8787/_spillway/", help="dashboard URL")
    parser.add_argument("--out-dir", type=Path, default=Path("docs/images"), help="output directory")
    parser.add_argument("--langs", nargs="+", default=list(_LOCALES), choices=list(_LOCALES), help="languages")
    parser.add_argument("--width", type=int, default=1060, help="viewport width in CSS pixels")
    parser.add_argument("--scale", type=float, default=1.0, help="device scale factor")
    parser.add_argument("--channel", default="chrome", help="browser channel (chrome / msedge / chromium)")
    return parser.parse_args()


def main() -> None:
    """Capture one full-page screenshot per language.

    言語ごとにページ全体のスクリーンショットを1枚撮る。
    """
    args = _parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=None if args.channel == "chromium" else args.channel, headless=True
        )
        for lang in args.langs:
            # 新しいコンテキストごとに保存済みの言語選択(localStorage)は空なので、
            # 表示言語は Accept-Language で決まる。
            # Each fresh context has no saved language choice in localStorage,
            # so Accept-Language decides the display language.
            context = browser.new_context(
                locale=_LOCALES[lang],
                viewport={"width": args.width, "height": 400},
                device_scale_factor=args.scale,
                extra_http_headers={"Accept-Language": _LOCALES[lang]},
            )
            page = context.new_page()
            page.goto(args.url, wait_until="networkidle")
            # 最初のステータス取得が終わり、カードが表示されるまで待つ。
            # Wait for the first status poll to fill in and reveal the cards.
            page.wait_for_selector("#grid:not([hidden])")
            out = args.out_dir / f"dashboard-{lang}.png"
            page.screenshot(path=str(out), full_page=True)
            print(f"wrote {out}")
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
