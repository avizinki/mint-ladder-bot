"""
Sniper engine: entry-side pipeline for Solana.

Pipeline: launch detection → token filtering → sniper decision → buy execution → create lot → hand off to mint-ladder-bot.

mint-ladder-bot remains the sell engine and state authority; the sniper creates lots in the same state format.
"""

from .launch_detector import (
    LaunchCandidate,
    detect_pump_fun,
    detect_raydium,
    detect_meteora,
    detect_jupiter_routes,
    detect_all,
)
from .token_filter import FilterResult, filter_candidate
from .sniper_executor import SniperExecutionResult, build_swap, execute_buy, confirm_fill
from .integration import ensure_mint_in_state, create_lot_in_state, persist_sniper_lot

__all__ = [
    "LaunchCandidate",
    "detect_pump_fun",
    "detect_raydium",
    "detect_meteora",
    "detect_jupiter_routes",
    "detect_all",
    "FilterResult",
    "filter_candidate",
    "SniperExecutionResult",
    "build_swap",
    "execute_buy",
    "confirm_fill",
    "ensure_mint_in_state",
    "create_lot_in_state",
    "persist_sniper_lot",
]
