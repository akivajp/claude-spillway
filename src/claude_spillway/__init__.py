"""claude-spillway: quota-aware failover proxy for Claude Code.

claude-spillway: Claude Code用のquota連動フェイルオーバープロキシ。
"""

from .cli import main

__all__ = ["main"]
