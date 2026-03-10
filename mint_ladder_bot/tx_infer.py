from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import EntryInfo
from .rpc import RpcClient, RpcError


logger = logging.getLogger(__name__)


@dataclass
class SellEvent:
    """One sell event: token out, SOL in for a tracked mint."""

    signature: str
    mint: str
    sold_raw: int
    sol_in_lamports: int
    block_time: Optional[datetime] = None

# Result: (signature, price_sol_per_token, block_time)
BuyTxResult = Tuple[str, float, Optional[datetime]]
# Multi-tx result: list of BuyTxResult for txs that sum to the target delta
BuyTxListResult = List[BuyTxResult]


def _extract_wallet_index(tx: dict, wallet: str) -> Optional[int]:
    message = tx.get("transaction", {}).get("message", {})
    account_keys = message.get("accountKeys") or []
    for idx, key in enumerate(account_keys):
        if isinstance(key, str):
            if key == wallet:
                return idx
        else:
            if key.get("pubkey") == wallet:
                return idx
    return None


def _parse_token_deltas_for_mints(
    tx: dict,
    wallet: str,
    mints: Sequence[str],
) -> Dict[str, int]:
    """
    Return raw token delta (post - pre) per mint, summed over all token
    accounts owned by `wallet` for that mint.
    """

    if tx is None:
        # Defensive: treat missing tx as zero delta for all requested mints.
        return {m: 0 for m in mints}

    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    # Primary owner resolution uses the explicit `owner` field. For some
    # routes / RPC shapes, the wallet owner may instead be implied via
    # `accountIndex` pointing at the wallet in accountKeys; use that as a
    # fallback so buys that credit the wallet ATA via index mapping are
    # still detected.
    wallet_idx = _extract_wallet_index(tx, wallet)

    pre_map: Dict[Tuple[str, str], int] = {}
    post_map: Dict[Tuple[str, str], int] = {}

    wanted = set(mints)

    for e in pre:
        owner = e.get("owner")
        mint = e.get("mint")
        if mint not in wanted:
            continue
        # Accept balances owned by the wallet either via explicit owner
        # field or via accountIndex matching the wallet's account key.
        if owner != wallet:
            if wallet_idx is None:
                continue
            try:
                acct_idx = e.get("accountIndex")
                if acct_idx is None or int(acct_idx) != wallet_idx:
                    continue
            except (TypeError, ValueError):
                continue
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
        mint = e.get("mint")
        if mint not in wanted:
            continue
        if owner != wallet:
            if wallet_idx is None:
                continue
            try:
                acct_idx = e.get("accountIndex")
                if acct_idx is None or int(acct_idx) != wallet_idx:
                    continue
            except (TypeError, ValueError):
                continue
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

    deltas: Dict[str, int] = {m: 0 for m in wanted}
    for (owner, mint) in set(list(pre_map.keys()) + list(post_map.keys())):
        pre_raw = pre_map.get((owner, mint), 0)
        post_raw = post_map.get((owner, mint), 0)
        deltas[mint] += post_raw - pre_raw

    return deltas


def _parse_token_deltas_for_wallet_all_mints(tx: dict, wallet: str) -> Dict[str, int]:
    """
    Return raw token delta (post - pre) per mint for the wallet, for every mint
    that appears in pre/post token balances. Used to detect token→token swaps
    (any mint with negative delta = spent, positive = received).
    """
    if tx is None:
        return {}

    meta = tx.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []

    pre_map: Dict[Tuple[str, str], int] = {}
    post_map: Dict[Tuple[str, str], int] = {}

    for e in pre:
        owner = e.get("owner")
        mint = e.get("mint")
        if owner != wallet:
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
        mint = e.get("mint")
        if owner != wallet:
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
    for (owner, mint) in set(list(pre_map.keys()) + list(post_map.keys())):
        pre_raw = pre_map.get((owner, mint), 0)
        post_raw = post_map.get((owner, mint), 0)
        deltas[mint] = post_raw - pre_raw
    return deltas


def _parse_sol_delta_lamports(tx: dict, wallet: str) -> Optional[int]:
    if tx is None:
        return None

    meta = tx.get("meta") or {}
    pre_balances = meta.get("preBalances") or []
    post_balances = meta.get("postBalances") or []
    wallet_idx = _extract_wallet_index(tx, wallet)
    if wallet_idx is None:
        return None
    try:
        pre_val = int(pre_balances[wallet_idx])
        post_val = int(post_balances[wallet_idx])
    except (IndexError, ValueError, TypeError):
        return None
    return post_val - pre_val


def _get_block_time(tx: dict) -> Optional[datetime]:
    block_time = tx.get("blockTime")
    if block_time is None:
        return None
    try:
        return datetime.fromtimestamp(block_time, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def parse_buy_fill_from_tx(
    tx: dict,
    wallet: str,
    mint: str,
    decimals: int = 6,
) -> Optional[Tuple[int, float]]:
    """
    Parse a confirmed buy tx (SOL -> token) for one mint.
    Returns (token_delta_raw, entry_price_sol_per_token) or None if not a valid buy for this mint.
    Used by sniper confirm_fill.
    """
    token_deltas = _parse_token_deltas_for_mints(tx, wallet, [mint])
    token_delta_raw = token_deltas.get(mint, 0)
    if token_delta_raw <= 0:
        return None
    sol_delta = _parse_sol_delta_lamports(tx, wallet)
    if sol_delta is None or sol_delta >= 0:
        return None
    meta = tx.get("meta") or {}
    fee = int(meta.get("fee") or 0)
    sol_spent_lamports = abs(sol_delta) - fee
    if sol_spent_lamports <= 0:
        return None
    try:
        sol_spent = sol_spent_lamports / 1e9
        entry_price_sol_per_token = sol_spent * (10 ** decimals) / float(token_delta_raw)
    except ZeroDivisionError:
        return None
    return (token_delta_raw, entry_price_sol_per_token)


def parse_sell_events_from_tx(
    tx: dict,
    wallet: str,
    mints_tracked: Set[str],
    signature: str,
) -> List[SellEvent]:
    """
    Parse tx for sell events: token balance decreased for a tracked mint, SOL increased.
    Returns one SellEvent per (mint, sold_raw); sol_in_lamports is total SOL gain for the tx
    (attributed to first mint when multiple mints sold in one tx).
    """
    token_deltas = _parse_token_deltas_for_mints(tx, wallet, list(mints_tracked))
    sol_delta = _parse_sol_delta_lamports(tx, wallet)
    if sol_delta is None or sol_delta <= 0:
        return []
    sol_in_lamports = sol_delta
    block_time = _get_block_time(tx)
    events: List[SellEvent] = []
    total_sold = 0
    for mint, delta in token_deltas.items():
        if mint not in mints_tracked or delta >= 0:
            continue
        events.append(
            SellEvent(
                signature=signature,
                mint=mint,
                sold_raw=abs(delta),
                sol_in_lamports=sol_in_lamports,
                block_time=block_time,
            )
        )
        total_sold += abs(delta)
    if events and total_sold > 0 and len(events) > 1:
        for ev in events:
            ev.sol_in_lamports = int(round(sol_in_lamports * (ev.sold_raw / total_sold)))
    return events


def infer_entries_for_mints(
    wallet_pubkey: str,
    mints: Sequence[str],
    signatures: Iterable[dict],
    rpc: RpcClient,
    decimals_by_mint: Optional[Dict[str, int]] = None,
) -> Dict[str, EntryInfo]:
    """
    Best-effort inference of entry price per mint from recent wallet transactions.

    For each mint, we look for the most recent transaction where:
      - Token balance for that mint (owned by wallet) increased.
      - SOL balance for the wallet decreased.
      - Exactly one of the target mints has a positive token delta.
    """

    inferred: Dict[str, EntryInfo] = {}
    remaining = set(mints)
    failures = 0

    for sig_info in signatures:
        if not remaining:
            break
        signature = sig_info.get("signature")
        if not signature:
            continue

        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:  # best-effort: log and limit retries
            logger.debug("Failed to fetch transaction %s: %s", signature, exc)
            failures += 1
            # If the RPC is unhappy (e.g. rate-limited), stop early and mark the
            # remaining mints as unknown rather than hammering the endpoint.
            if failures >= 3 or isinstance(exc, RpcError):
                logger.warning(
                    "Stopping entry-price inference after %d failures; "
                    "remaining mints will have unknown entry price.",
                    failures,
                )
                break
            continue

        token_deltas = _parse_token_deltas_for_mints(tx, wallet_pubkey, list(remaining))
        positive_mints = [m for m, d in token_deltas.items() if d > 0]

        # Require exactly one mint to increase to avoid ambiguous multi-mint swaps.
        if len(positive_mints) != 1:
            continue

        mint = positive_mints[0]
        token_delta_raw = token_deltas[mint]
        if token_delta_raw <= 0:
            continue

        sol_delta = _parse_sol_delta_lamports(tx, wallet_pubkey)
        if sol_delta is None or sol_delta >= 0:
            # We need SOL to go down (spent) to treat this as a buy.
            continue

        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        sol_spent_lamports = abs(sol_delta) - fee
        if sol_spent_lamports <= 0:
            continue

        try:
            # Price must be in SOL per human token (10^decimals raw units),
            # consistent with find_buy_tx_for_delta and lot entry semantics.
            dec = (decimals_by_mint or {}).get(mint, 6)
            if dec < 0:
                dec = 0
            sol_spent = sol_spent_lamports / 1e9
            price = sol_spent * (10 ** dec) / float(token_delta_raw)
        except (ZeroDivisionError, OverflowError, ValueError):
            continue

        when = _get_block_time(tx) or datetime.now(tz=timezone.utc)

        inferred[mint] = EntryInfo(
            mode="auto",
            entry_price_sol_per_token=price,
            entry_source="inferred_from_tx",
            entry_tx_signature=signature,
        )
        remaining.discard(mint)

    # For mints we couldn't infer, set unknown entry with price 0.
    for mint in remaining:
        inferred[mint] = EntryInfo(
            mode="auto",
            entry_price_sol_per_token=0.0,
            entry_source="unknown",
            entry_tx_signature=None,
        )

    return inferred


def find_buy_tx_for_delta(
    wallet_pubkey: str,
    mint: str,
    delta_raw: int,
    rpc: RpcClient,
    max_signatures: int = 30,
    exclude_signatures: Optional[Set[str]] = None,
    decimals: int = 6,
    failure_reason_out: Optional[list] = None,
) -> Optional[BuyTxResult]:
    """
    Find the wallet transaction that added exactly delta_raw of the given mint (buy).
    Returns (signature, price_sol_per_human_token, block_time) or None if not found.
    Price is in SOL per human token (i.e. per 10^decimals raw units).
    If exclude_signatures is set, any signature already used for another lot is skipped
    so the same tx is not assigned to multiple lots.
    If failure_reason_out is a list and no tx is found, appends one of:
    tx_not_found, delta_mismatch, scan_window_exceeded, rpc_error.
    """
    def set_reason(r: str) -> None:
        if failure_reason_out is not None:
            failure_reason_out.append(r)

    if delta_raw <= 0:
        set_reason("tx_not_found")
        return None
    excluded = exclude_signatures or set()
    try:
        sig_list = rpc.get_signatures_for_address(wallet_pubkey, limit=max_signatures)
    except Exception as exc:
        logger.warning("TX_LOOKUP_FAILED mint=%s reason=get_signatures_for_address error=%s", mint[:12], exc)
        set_reason("rpc_error")
        return None
    if not sig_list:
        logger.info("TX_LOOKUP_FAILED mint=%s delta_raw=%s reason=no_signatures", mint[:12], delta_raw)
        set_reason("tx_not_found")
        return None
    failures = 0
    saw_delta_mismatch = False
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        if signature in excluded:
            logger.debug("find_buy_tx_for_delta skipping already-used sig=%s", signature[:16])
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            logger.debug("find_buy_tx_for_delta get_transaction %s failed: %s", signature[:16], exc)
            failures += 1
            if failures >= 3 or isinstance(exc, RpcError):
                logger.info("TX_LOOKUP_FAILED mint=%s reason=get_transaction_failures", mint[:12])
                set_reason("rpc_error")
                break
            continue
        if not tx:
            continue
        token_deltas = _parse_token_deltas_for_mints(tx, wallet_pubkey, [mint])
        token_delta = token_deltas.get(mint, 0)
        # Allow 1% tolerance for rounding / RPC representation.
        tolerance = max(1, int(delta_raw * 0.01))
        if abs(token_delta - delta_raw) > tolerance:
            saw_delta_mismatch = True
            logger.info(
                "TX_DELTA_NOT_MATCHED mint=%s sig=%s tx_delta=%s wanted=%s tolerance=%s",
                mint[:12], signature[:16], token_delta, delta_raw, tolerance,
            )
            continue
        sol_delta = _parse_sol_delta_lamports(tx, wallet_pubkey)
        if sol_delta is None or sol_delta >= 0:
            continue
        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        sol_spent_lamports = abs(sol_delta) - fee
        if sol_spent_lamports <= 0:
            continue
        try:
            sol_spent = sol_spent_lamports / 1e9
            # Entry price formula: price = sol_spent / token_amount (human units).
            # token_delta is raw (smallest units); human amount = token_delta / 10^decimals.
            # So: price = sol_spent / (token_delta / 10^decimals) = sol_spent * (10 ** decimals) / token_delta.
            # Decimals must match the mint (from status/config). Sub-MIN prices are downgraded to unknown in resolver.
            price = sol_spent * (10 ** decimals) / float(token_delta)
        except ZeroDivisionError:
            continue
        when = _get_block_time(tx)
        logger.info(
            "LOT_TX_MATCHED mint=%s delta_raw=%s sig=%s price=%.6e (LOT_PRICE_FROM_TX)",
            mint[:12], delta_raw, signature[:16], price,
        )
        return (signature, price, when)
    if len(sig_list) >= max_signatures:
        logger.info(
            "TX_SCAN_LIMIT_REACHED mint=%s delta_raw=%s scanned=%s",
            mint[:12], delta_raw, len(sig_list),
        )
        set_reason("scan_window_exceeded")
    else:
        set_reason("delta_mismatch" if saw_delta_mismatch else "tx_not_found")
    logger.info(
        "TX_LOOKUP_FAILED mint=%s delta_raw=%s reason=no_matching_tx_after_scan",
        mint[:12], delta_raw,
    )
    return None


def find_buy_txs_for_delta_sum(
    wallet_pubkey: str,
    mint: str,
    delta_raw: int,
    rpc: RpcClient,
    max_signatures: int = 60,
    exclude_signatures: Optional[Set[str]] = None,
    decimals: int = 6,
    max_combination_size: int = 5,
) -> Optional[BuyTxListResult]:
    """
    Find a set of wallet transactions whose token deltas for this mint sum to delta_raw (multi-tx match).
    Returns list of (signature, price_sol_per_token, block_time) for each tx in the set, or None.
    Prefers exact sum within 1% tolerance. Tries combination sizes 2 up to max_combination_size.
    """
    if delta_raw <= 0:
        return None
    tolerance = max(1, int(delta_raw * 0.01))
    excluded = exclude_signatures or set()
    try:
        sig_list = rpc.get_signatures_for_address(wallet_pubkey, limit=max_signatures)
    except Exception as exc:
        logger.warning(
            "MULTI_TX_LOOKUP mint=%s reason=get_signatures_for_address error=%s",
            mint[:12], exc,
        )
        return None
    if not sig_list:
        return None
    # Collect candidates: (sig, token_delta, sol_spent_lamports, block_time)
    candidates: List[Tuple[str, int, int, Optional[datetime]]] = []
    failures = 0
    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature or signature in excluded:
            continue
        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            logger.debug("find_buy_txs_for_delta_sum get_transaction %s failed: %s", signature[:16], exc)
            failures += 1
            if failures >= 3:
                break
            continue
        if not tx:
            continue
        token_deltas = _parse_token_deltas_for_mints(tx, wallet_pubkey, [mint])
        token_delta = token_deltas.get(mint, 0)
        if token_delta <= 0:
            continue
        sol_delta = _parse_sol_delta_lamports(tx, wallet_pubkey)
        if sol_delta is None or sol_delta >= 0:
            continue
        meta = tx.get("meta") or {}
        fee = int(meta.get("fee") or 0)
        sol_spent_lamports = abs(sol_delta) - fee
        if sol_spent_lamports <= 0:
            continue
        when = _get_block_time(tx)
        candidates.append((signature, token_delta, sol_spent_lamports, when))
    if not candidates:
        return None
    # Try combinations of size 2, 3, ..., max_combination_size
    for k in range(2, min(max_combination_size + 1, len(candidates) + 1)):
        for combo in itertools.combinations(candidates, k):
            total_delta = sum(c[1] for c in combo)
            if abs(total_delta - delta_raw) <= tolerance:
                # Build result with price per tx: sol_spent / (token_delta / 10^decimals)
                result: BuyTxListResult = []
                for sig, tok_d, sol_lam, when in combo:
                    try:
                        sol_spent = sol_lam / 1e9
                        price = sol_spent * (10 ** decimals) / float(tok_d)
                    except ZeroDivisionError:
                        continue
                    result.append((sig, price, when))
                if len(result) == len(combo):
                    logger.info(
                        "MULTI_TX_DELTA_MATCHED mint=%s delta_raw=%s n_txs=%s sigs=%s",
                        mint[:12], delta_raw, len(result), [r[0][:16] for r in result],
                    )
                    return result
    return None

