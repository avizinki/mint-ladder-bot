"""
Read-only transfer-provenance analysis (Step 2 of launch-time reconstruction design).

Detects transfer-in candidates: wallet token balance increases that are NOT
recognized as SOL→token or token→token swaps. Classifies each candidate as
trusted-transfer-candidate, untrusted-transfer-candidate, ambiguous, or likely-swap.

Does NOT create lots, mutate state, or change trading behavior.
Design: docs/trading/launch-time-reconstruction-transfer-provenance-design.md
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .tx_infer import _get_block_time
from .tx_lot_engine import _parse_buy_events_from_tx

logger = logging.getLogger(__name__)

# Classification labels (contract for CLI and tests)
CLASS_TRUSTED_TRANSFER_CANDIDATE = "trusted-transfer-candidate"
CLASS_UNTRUSTED_TRANSFER_CANDIDATE = "untrusted-transfer-candidate"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_LIKELY_SWAP = "likely-swap"


def _token_deltas_by_owner_for_mint(tx: dict, mint: str) -> Dict[str, int]:
    """
    Return raw token delta (post - pre) per owner for the given mint.
    Used to find source wallet: owner(s) with negative delta sent tokens.
    """
    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    pre_map: Dict[Tuple[str, str], int] = {}
    post_map: Dict[Tuple[str, str], int] = {}

    for e in pre:
        owner = e.get("owner")
        m = e.get("mint")
        if m != mint or not owner:
            continue
        ui = e.get("uiTokenAmount") or {}
        amt = ui.get("amount")
        if amt is None:
            continue
        try:
            raw = int(amt)
        except ValueError:
            continue
        pre_map[(owner, mint)] = raw

    for e in post:
        owner = e.get("owner")
        m = e.get("mint")
        if m != mint or not owner:
            continue
        ui = e.get("uiTokenAmount") or {}
        amt = ui.get("amount")
        if amt is None:
            continue
        try:
            raw = int(amt)
        except ValueError:
            continue
        post_map[(owner, mint)] = raw

    deltas: Dict[str, int] = {}
    for (owner, _) in set(list(pre_map.keys()) + list(post_map.keys())):
        pre_raw = pre_map.get((owner, mint), 0)
        post_raw = post_map.get((owner, mint), 0)
        deltas[owner] = post_raw - pre_raw
    return deltas


def _derive_source_wallet_from_transfer_tx(
    tx: dict,
    wallet: str,
    mint: str,
    amount_raw_increase: int,
) -> Optional[str]:
    """
    For a tx where wallet received amount_raw_increase of mint (and we've already
    decided it's not a swap), find the sender. Returns the owner whose balance
    decreased by that amount, or None if ambiguous (multiple senders or no clear single source).
    """
    deltas = _token_deltas_by_owner_for_mint(tx, mint)
    # Our wallet should have +amount_raw_increase
    if deltas.get(wallet, 0) != amount_raw_increase:
        return None
    # Find owner(s) with negative delta summing to -amount_raw_increase
    senders = [(o, -d) for o, d in deltas.items() if o != wallet and d < 0]
    if not senders:
        return None
    total_sent = sum(amt for _, amt in senders)
    if total_sent != amount_raw_increase:
        return None
    # Single sender: clear source
    if len(senders) == 1:
        return senders[0][0]
    # Multiple senders: ambiguous (do not pick one)
    return None


@dataclass
class TransferProvenanceCandidate:
    """One transfer-in candidate (read-only report row)."""

    mint: str
    symbol: Optional[str]
    tx_signature: str
    slot: Optional[int]
    block_time: Optional[datetime]
    amount_raw: int
    source_wallet: Optional[str]
    classification: str  # CLASS_* constant
    reason: str
    source_in_trusted_list: bool
    could_help_reconciliation: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mint": self.mint,
            "symbol": self.symbol,
            "tx_signature": self.tx_signature,
            "slot": self.slot,
            "block_time": self.block_time.isoformat() if self.block_time else None,
            "amount_raw": self.amount_raw,
            "source_wallet": self.source_wallet,
            "classification": self.classification,
            "reason": self.reason,
            "source_in_trusted_list": self.source_in_trusted_list,
            "could_help_reconciliation": self.could_help_reconciliation,
        }


def run_transfer_provenance_analysis(
    wallet: str,
    mints_tracked: Set[str],
    rpc: Any,
    max_signatures: int,
    trusted_source_wallets: List[str],
    decimals_by_mint: Dict[str, int],
    symbol_by_mint: Optional[Dict[str, str]] = None,
    mint_filter: Optional[Set[str]] = None,
) -> List[TransferProvenanceCandidate]:
    """
    Read-only analysis: scan wallet tx history, find token balance increases that
    are not explained by existing swap parsing, classify as transfer-in candidate
    (trusted / untrusted / ambiguous) or likely-swap.

    Does NOT create lots or mutate state.
    """
    trusted_set = set(trusted_source_wallets or [])
    symbol_by_mint = symbol_by_mint or {}
    decimals_by_mint = decimals_by_mint or {}
    mints_to_consider = mints_tracked & mint_filter if mint_filter else mints_tracked
    if not mints_to_consider:
        return []

    # Fetch bounded tx history
    sig_list: List[Dict[str, Any]] = []
    try:
        batch = rpc.get_signatures_for_address(wallet, limit=min(max_signatures, 500))
        if isinstance(batch, list):
            sig_list = batch[:max_signatures]
    except Exception as exc:
        logger.warning("transfer_provenance get_signatures_for_address failed: %s", exc)
        return []

    results: List[TransferProvenanceCandidate] = []
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            logger.debug("get_transaction %s failed: %s", signature[:16], exc)
            continue
        if not tx or not isinstance(tx, dict):
            continue

        meta = tx.get("meta") or {}
        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []
        # Wallet token deltas for this tx (all mints)
        pre_w: Dict[str, int] = {}
        post_w: Dict[str, int] = {}
        for e in pre:
            if e.get("owner") != wallet:
                continue
            m = e.get("mint")
            if m not in mints_to_consider:
                continue
            ui = e.get("uiTokenAmount") or {}
            amt = ui.get("amount")
            if amt is None:
                continue
            try:
                pre_w[m] = int(amt)
            except ValueError:
                pass
        for e in post:
            if e.get("owner") != wallet:
                continue
            m = e.get("mint")
            if m not in mints_to_consider:
                continue
            ui = e.get("uiTokenAmount") or {}
            amt = ui.get("amount")
            if amt is None:
                continue
            try:
                post_w[m] = int(amt)
            except ValueError:
                pass

        for mint in mints_to_consider:
            post_raw = post_w.get(mint, 0)
            pre_raw = pre_w.get(mint, 0)
            delta = post_raw - pre_raw
            if delta <= 0:
                continue

            # Wallet received delta of mint. Is this a swap or a transfer?
            dec = decimals_by_mint.get(mint, 6)
            buy_events = _parse_buy_events_from_tx(
                tx, wallet, signature, mints_tracked, decimals_by_mint
            )
            # Any buy event for this mint from this tx?
            mint_buy_events = [e for e in buy_events if e.mint == mint]
            if mint_buy_events:
                # Recognized as swap
                results.append(
                    TransferProvenanceCandidate(
                        mint=mint,
                        symbol=symbol_by_mint.get(mint),
                        tx_signature=signature,
                        slot=tx.get("slot"),
                        block_time=_get_block_time(tx),
                        amount_raw=delta,
                        source_wallet=None,
                        classification=CLASS_LIKELY_SWAP,
                        reason="tx_parsed_as_swap",
                        source_in_trusted_list=False,
                        could_help_reconciliation=False,
                    )
                )
                continue

            # Not a swap → transfer-in candidate. Derive source.
            source_wallet = _derive_source_wallet_from_transfer_tx(
                tx, wallet, mint, delta
            )
            source_in_trusted = (
                source_wallet is not None and source_wallet in trusted_set
            )
            if source_wallet is None:
                classification = CLASS_AMBIGUOUS
                reason = "source_wallet_not_derivable"
            elif source_in_trusted:
                classification = CLASS_TRUSTED_TRANSFER_CANDIDATE
                reason = "source_in_trusted_source_wallets"
            else:
                classification = CLASS_UNTRUSTED_TRANSFER_CANDIDATE
                reason = "source_not_in_trusted_source_wallets"

            # Could help reconciliation: if we had a lot for this transfer we'd explain more balance
            could_help = True  # any unexplained inflow could theoretically help

            results.append(
                TransferProvenanceCandidate(
                    mint=mint,
                    symbol=symbol_by_mint.get(mint),
                    tx_signature=signature,
                    slot=tx.get("slot"),
                    block_time=_get_block_time(tx),
                    amount_raw=delta,
                    source_wallet=source_wallet,
                    classification=classification,
                    reason=reason,
                    source_in_trusted_list=source_in_trusted,
                    could_help_reconciliation=could_help,
                )
            )

    return results
