"""
Symbol cache and resolver for dashboard/UI: mint -> human-readable symbol.

Priority: state symbol -> status.json token metadata -> symbol_cache.json -> short mint.
Never blocks runtime on lookup failure.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"


def short_mint(mint: str, length: int = 8) -> str:
    """Short mint for display (e.g. x95H…WKyp)."""
    if not mint or len(mint) <= length:
        return mint or "?"
    return mint[:4] + "…" + mint[-4:]


def _short_mint(mint: str, length: int = 8) -> str:
    return short_mint(mint, length)


def load_symbol_cache(data_dir: Path) -> Dict[str, str]:
    """Load runtime/symbol_cache.json. Returns {} on missing or error. Never raises."""
    cache_path = data_dir / "runtime" / "symbol_cache.json"
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("symbol_cache load failed: %s", e)
        return {}


def save_symbol_cache(data_dir: Path, cache: Dict[str, str]) -> None:
    """Write symbol cache to runtime/symbol_cache.json. No-op on error (never block)."""
    cache_path = data_dir / "runtime" / "symbol_cache.json"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("symbol_cache save failed: %s", e)


def resolve_symbol(
    mint: str,
    state_mint_data: Optional[Dict[str, Any]] = None,
    status_mint_data: Optional[Dict[str, Any]] = None,
    cache: Optional[Dict[str, str]] = None,
) -> str:
    """
    Resolve mint to display symbol. Priority:
    1) state symbol (output_asset_symbol or symbol on mint data)
    2) status.json token metadata (symbol)
    3) cached symbol
    4) WSOL -> "SOL", else short mint
    """
    if mint == WSOL_MINT:
        return "SOL"
    if state_mint_data:
        sym = state_mint_data.get("output_asset_symbol") or state_mint_data.get("symbol")
        if sym and str(sym).strip():
            return str(sym).strip()
    if status_mint_data:
        sym = status_mint_data.get("symbol")
        if sym and str(sym).strip():
            return str(sym).strip()
    if cache and mint in cache:
        s = cache.get(mint)
        if s and str(s).strip():
            return str(s).strip()
    return _short_mint(mint)


def ensure_symbol_cached(data_dir: Path, mint: str, symbol: str) -> None:
    """If symbol is not empty and not short-mint-only, add to cache. Never blocks."""
    if not mint or not symbol or symbol == _short_mint(mint):
        return
    cache = load_symbol_cache(data_dir)
    if mint not in cache or cache[mint] != symbol:
        cache[mint] = symbol
        save_symbol_cache(data_dir, cache)
