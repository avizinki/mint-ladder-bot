from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from .tx_lot_engine import _parse_buy_events_from_tx
from .tx_infer import parse_sell_events_from_tx


@dataclass
class MintSourceGapReport:
    mint: str
    symbol: Optional[str]
    total_signatures_scanned: int
    signatures_mentioning_mint_any: int
    signatures_with_wallet_mint_balances: int
    signatures_with_buy_events: int
    signatures_with_sell_events: int
    signatures_with_mint_no_event: int
    diagnosis_category: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "total_signatures_scanned": self.total_signatures_scanned,
            "signatures_mentioning_mint_any": self.signatures_mentioning_mint_any,
            "signatures_with_wallet_mint_balances": self.signatures_with_wallet_mint_balances,
            "signatures_with_buy_events": self.signatures_with_buy_events,
            "signatures_with_sell_events": self.signatures_with_sell_events,
            "signatures_with_mint_no_event": self.signatures_with_mint_no_event,
            "diagnosis_category": self.diagnosis_category,
        }


def _tx_mentions_mint_any(tx: dict, mint: str) -> bool:
    meta = tx.get("meta") or {}
    for key in ("preTokenBalances", "postTokenBalances"):
        for bal in meta.get(key) or []:
            if bal.get("mint") == mint:
                return True
    return False


def _tx_mentions_mint_wallet_balance(tx: dict, wallet: str, mint: str) -> bool:
    meta = tx.get("meta") or {}
    for key in ("preTokenBalances", "postTokenBalances"):
        for bal in meta.get(key) or []:
            if bal.get("owner") == wallet and bal.get("mint") == mint:
                return True
    return False


def analyze_source_gap_for_mint(
    wallet: str,
    mint: str,
    txs: Sequence[dict],
    decimals_by_mint: Dict[str, int],
    symbol: Optional[str] = None,
) -> MintSourceGapReport:
    """
    Read-only source-gap analysis for a single mint over a fixed wallet tx corpus.

    For each signature, tracks:
    - whether the mint appears anywhere in token balances
    - whether the mint appears in wallet-owned token balances
    - whether existing parsers emit buy/sell events for the mint
    """
    mints_tracked: Set[str] = {mint}

    sigs_any: Set[str] = set()
    sigs_wallet: Set[str] = set()
    sigs_buy: Set[str] = set()
    sigs_sell: Set[str] = set()

    for tx in txs:
        signature = tx.get("signature")
        if not signature:
            continue
        sig = str(signature)

        # Mint mentions anywhere in token balances.
        if _tx_mentions_mint_any(tx, mint):
            sigs_any.add(sig)

        # Wallet-owned balances mentioning mint.
        if _tx_mentions_mint_wallet_balance(tx, wallet, mint):
            sigs_wallet.add(sig)

        # Existing buy/sell parsers.
        try:
            buy_events = _parse_buy_events_from_tx(
                tx=tx,
                wallet=wallet,
                signature=sig,
                mints_tracked=mints_tracked,
                decimals_by_mint=decimals_by_mint,
            )
        except Exception:
            buy_events = []
        if any(getattr(ev, "mint", None) == mint for ev in buy_events):
            sigs_buy.add(sig)

        try:
            sell_events = parse_sell_events_from_tx(
                tx=tx,
                wallet=wallet,
                mints_tracked=mints_tracked,
                signature=sig,
            )
        except Exception:
            sell_events = []
        if any(getattr(ev, "mint", None) == mint for ev in sell_events):
            sigs_sell.add(sig)

    total_signatures_scanned = len(txs)
    signatures_mentioning_mint_any = len(sigs_any)
    signatures_with_wallet_mint_balances = len(sigs_wallet)
    signatures_with_buy_events = len(sigs_buy)
    signatures_with_sell_events = len(sigs_sell)
    signatures_with_mint_no_event = len(
        sigs_any - sigs_buy - sigs_sell
    )

    # Diagnosis heuristic:
    # - No mentions at all -> source gap likely
    # - Mentions without wallet ownership -> transfer provenance likely
    # - Wallet-owned balances but no buy/sell events -> parser gap likely
    # - Mixed evidence -> inconclusive
    if signatures_mentioning_mint_any == 0:
        category = "source gap likely"
    elif signatures_with_wallet_mint_balances == 0:
        category = "transfer provenance likely"
    elif signatures_with_wallet_mint_balances > 0 and signatures_with_buy_events == 0 and signatures_with_sell_events == 0:
        category = "parser gap likely"
    else:
        category = "inconclusive"

    return MintSourceGapReport(
        mint=mint,
        symbol=symbol,
        total_signatures_scanned=total_signatures_scanned,
        signatures_mentioning_mint_any=signatures_mentioning_mint_any,
        signatures_with_wallet_mint_balances=signatures_with_wallet_mint_balances,
        signatures_with_buy_events=signatures_with_buy_events,
        signatures_with_sell_events=signatures_with_sell_events,
        signatures_with_mint_no_event=signatures_with_mint_no_event,
        diagnosis_category=category,
    )

