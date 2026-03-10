"""
Transaction-first lot engine.

Ingests wallet transaction history, parses buy transactions, and creates lots
directly from transaction data. Idempotent: a tx signature is processed at most
once per mint (one lot per (signature, mint)); state holds tx_signature on lots.

Entry price invariant:
  entry_price_sol_per_token = quote_value_sol / token_amount only.
  Quote value may come from: SOL delta, source lot FIFO cost (token→token),
  WSOL equivalent, or (future) quote token converted via pool price at tx time.
  Market price must NEVER be used as entry during reconstruction (would make
  all token→token lots from different swaps show the same entry).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .models import LotInfo, RuntimeMintState, RuntimeState
from .rpc import RpcClient, RpcError
from .tx_infer import (
    _get_block_time,
    _parse_sol_delta_lamports,
    _parse_token_deltas_for_mints,
    _parse_token_deltas_for_wallet_all_mints,
)

logger = logging.getLogger(__name__)

ENTRY_PRICE_MIN = 1e-12
ENTRY_PRICE_MAX = 1e3

# Wrapped SOL: token→token where input is WSOL can use SOL cost basis (1:1 lamports).
WSOL_MINT = "So11111111111111111111111111111111111111112"

SwapType = str  # "sol_to_token" | "token_to_token" | "token_to_sol" | "multi_hop"


def _validate_entry_price(price: Optional[float]) -> bool:
    if price is None:
        return False
    if price <= 0 or price < ENTRY_PRICE_MIN or price > ENTRY_PRICE_MAX:
        return False
    return True


def _source_cost_basis_sol(
    state: RuntimeState,
    source_mint: str,
    amount_raw: int,
    decimals_by_mint: Dict[str, int],
) -> Optional[Tuple[float, str]]:
    """
    FIFO cost basis in SOL for amount_raw of source_mint using state lots.
    Returns (sol_value, valuation_method) or None if not computable.
    """
    ms = state.mints.get(source_mint)
    if not ms:
        return None
    lots = getattr(ms, "lots", None) or []
    if not lots:
        return None
    dec = decimals_by_mint.get(source_mint, 6)
    remaining = amount_raw
    cost_sol = 0.0
    for lot in lots:
        if remaining <= 0:
            break
        if getattr(lot, "status", "active") != "active":
            continue
        rem = int(getattr(lot, "remaining_amount", 0) or 0)
        if rem <= 0:
            continue
        ep = getattr(lot, "entry_price_sol_per_token", None)
        # Invariant: token→token FIFO cost basis must align with actual FIFO disposals.
        # If we encounter an active lot with unknown/invalid entry before covering the
        # requested amount, we cannot compute a reliable FIFO cost for this disposal.
        if ep is None or not _validate_entry_price(ep):
            return None
        take = min(rem, remaining)
        token_human = take / (10 ** dec)
        cost_sol += token_human * ep
        remaining -= take
    if remaining > 0:
        return None
    return (cost_sol, "source_lot_cost")


def _mint_market_bootstrap_entry(
    state: RuntimeState,
    dest_mint: str,
) -> Optional[Tuple[float, str]]:
    """
    Destination mint's existing mint-level entry (bootstrap/market). Returns (price, method) or None.
    Must NOT be used for lot entry reconstruction: using market/bootstrap entry would make all
    token→token lots from different swaps show the same entry. Kept for potential display-only use.
    """
    ms = state.mints.get(dest_mint)
    if not ms:
        return None
    ep = getattr(ms, "working_entry_price_sol_per_token", None) or getattr(ms, "entry_price_sol_per_token", None)
    if ep is None or not _validate_entry_price(ep):
        return None
    return (ep, "mint_market_bootstrap")


def _enhanced_tx_swap_value(
    tx: dict,
    wallet: str,
    dest_mint: str,
    dest_amount_raw: int,
    decimals: int,
) -> Optional[Tuple[float, str]]:
    """
    Try to derive SOL value from enhanced tx metadata (e.g. Helius nativeBalanceChange / tokenBalanceChange).
    Returns (entry_price_sol_per_token, "enhanced_swap_amounts") or None.
    """
    # Helius enhanced: accountData may have nativeBalanceChange, tokenBalanceChange
    meta = tx.get("meta") or {}
    account_data = meta.get("accountData") or []
    # Some APIs put balance changes in meta.postTokenBalances / preTokenBalances; we already use those for deltas.
    # Check for explicit swap amounts in description or inner instructions if present.
    desc = tx.get("description") or ""
    if isinstance(desc, str) and "swap" in desc.lower():
        pass  # Could parse; skip for now to avoid fragile parsing
    return None


def _debit_lots_fifo_local(mint_state: RuntimeMintState, amount_raw: int) -> None:
    """Reduce lot remaining_amounts FIFO by amount_raw. Mark lots fully_sold when exhausted."""
    lots = getattr(mint_state, "lots", None) or []
    if not lots:
        return
    remaining_to_debit = amount_raw
    for lot in lots:
        if remaining_to_debit <= 0 or getattr(lot, "status", "active") != "active":
            continue
        rem = int(getattr(lot, "remaining_amount", 0) or 0)
        if rem <= 0:
            lot.status = "fully_sold"
            continue
        take = min(rem, remaining_to_debit)
        lot.remaining_amount = str(rem - take)
        remaining_to_debit -= take
        if int(lot.remaining_amount) <= 0:
            lot.status = "fully_sold"
        if remaining_to_debit <= 0:
            break


def _trading_bag_from_lots_local(mint_state: RuntimeMintState) -> int:
    """Sum remaining_amount of active lots from tx_exact/tx_parsed only."""
    lots = getattr(mint_state, "lots", None) or []
    total = 0
    for lot in lots:
        if getattr(lot, "status", "active") != "active":
            continue
        if getattr(lot, "source", None) not in ("tx_exact", "tx_parsed"):
            continue
        total += int(getattr(lot, "remaining_amount", 0) or 0)
    return total


@dataclass
class BuyEvent:
    """One buy event: mint received in a tx (SOL→token or token→token)."""

    signature: str
    mint: str
    token_amount_raw: int
    sol_spent_lamports: int  # 0 for token→token (unless WSOL in which case we convert)
    entry_price_sol_per_token: Optional[float]  # SOL per human token; None if unknown
    block_time: Optional[datetime]
    slot: Optional[int] = None
    # Swap classification and cost-basis metadata
    swap_type: SwapType = "sol_to_token"
    input_asset_mint: Optional[str] = None
    input_amount_raw: Optional[int] = None
    source_sold_raw: Optional[int] = None  # total source token raw sold in this tx (for debit)
    entry_price_usd_per_token: Optional[float] = None
    confidence: str = "known"  # known | inferred | unknown
    valuation_method: Optional[str] = None  # sol_spent | source_lot_cost | wsol_equivalent | unknown


def _parse_buy_events_from_tx(
    tx: dict,
    wallet: str,
    signature: str,
    mints_tracked: Set[str],
    decimals_by_mint: Dict[str, int],
) -> List[BuyEvent]:
    """
    Parse tx into buy events: SOL→token and/or token→token.
    - SOL→token: wallet SOL decrease, mint(s) in mints_tracked increase; entry_price_sol from SOL spent.
    - token→token: no SOL spent (or SOL increase); mint in mints_tracked increases, some other mint decreases.
      If single input is WSOL, treat as SOL equivalent for cost basis. Otherwise entry_price_sol = None, confidence unknown.
    """
    meta = tx.get("meta") or {}
    fee = int(meta.get("fee") or 0)
    sol_delta = _parse_sol_delta_lamports(tx, wallet)
    block_time = _get_block_time(tx)
    slot = tx.get("slot")

    # Prefer token→token when any token balance decreased (don't treat fee-only SOL decrease as SOL→token)
    all_deltas_early = _parse_token_deltas_for_wallet_all_mints(tx, wallet)
    token_inputs_early = [(m, d) for m, d in all_deltas_early.items() if d < 0]
    received_tracked_early = [m for m in mints_tracked if all_deltas_early.get(m, 0) > 0]
    is_token_to_token_candidate = len(token_inputs_early) > 0 and len(received_tracked_early) > 0

    # Path 2 first when tx has token→token shape (token out + token in)
    if is_token_to_token_candidate:
        all_deltas = all_deltas_early
        received = []
        for mint in mints_tracked:
            delta_raw = all_deltas.get(mint, 0)
            if delta_raw <= 0:
                continue
            dec = decimals_by_mint.get(mint, 6)
            token_human = delta_raw / (10 ** dec)
            if token_human <= 0:
                continue
            received.append((mint, delta_raw, token_human))
        if received:
            token_inputs = [(m, d) for m, d in all_deltas.items() if d < 0]
            sol_decrease = sol_delta is not None and sol_delta < 0
            sol_spent = (abs(sol_delta) - fee) if sol_decrease else 0
            num_inputs = len(token_inputs) + (1 if sol_decrease and sol_spent > 0 else 0)
            swap_type: SwapType = "multi_hop" if num_inputs > 1 else "token_to_token"
            total_received_human = sum(th for _, _, th in received)
            input_mint, total_input_raw = None, None
            if len(token_inputs) == 1:
                input_mint, input_neg = token_inputs[0]
                total_input_raw = abs(input_neg)
            out_events: List[BuyEvent] = []
            for mint, delta_raw, token_human in received:
                entry_sol = None
                confidence = "unknown"
                input_amount_raw = None
                fraction = token_human / total_received_human if total_received_human and total_received_human > 0 else 1.0
                valuation_method_ev = None
                if input_mint is not None and total_input_raw is not None:
                    input_amount_raw = int(total_input_raw * fraction)
                    if input_mint == WSOL_MINT:
                        sol_equiv_lamports = input_amount_raw
                        entry_sol = (sol_equiv_lamports / 1e9) / token_human
                        confidence = "inferred"
                        valuation_method_ev = "wsol_equivalent"
                    if entry_sol is not None and not _validate_entry_price(entry_sol):
                        entry_sol, confidence, valuation_method_ev = None, "unknown", None
                out_events.append(
                    BuyEvent(
                        signature=signature,
                        mint=mint,
                        token_amount_raw=delta_raw,
                        sol_spent_lamports=0,
                        entry_price_sol_per_token=entry_sol,
                        block_time=block_time,
                        slot=slot,
                        swap_type=swap_type,
                        input_asset_mint=input_mint,
                        input_amount_raw=input_amount_raw,
                        source_sold_raw=total_input_raw if (input_mint and total_input_raw) else None,
                        confidence=confidence,
                        valuation_method=valuation_method_ev or ("unknown" if swap_type in ("token_to_token", "multi_hop") else None),
                    )
                )
            return out_events

    # Path 1: SOL→token (no token input; SOL decrease → token increase)
    if sol_delta is not None and sol_delta < 0:
        sol_spent_lamports = abs(sol_delta) - fee
        if sol_spent_lamports > 0:
            token_deltas = _parse_token_deltas_for_mints(tx, wallet, list(mints_tracked))
            positive: List[Tuple[str, int, float]] = []
            total_human = 0.0
            for mint, delta_raw in token_deltas.items():
                if delta_raw <= 0 or mint not in mints_tracked:
                    continue
                dec = decimals_by_mint.get(mint, 6)
                token_human = delta_raw / (10 ** dec)
                if token_human <= 0:
                    continue
                total_human += token_human
                positive.append((mint, delta_raw, token_human))
            if positive and total_human > 0:
                sol_spent_sol = sol_spent_lamports / 1e9
                events: List[BuyEvent] = []
                for mint, delta_raw, token_human in positive:
                    fraction = token_human / total_human
                    sol_allocated = sol_spent_sol * fraction
                    try:
                        price = sol_allocated / token_human
                    except ZeroDivisionError:
                        continue
                    if _validate_entry_price(price):
                        events.append(
                            BuyEvent(
                                signature=signature,
                                mint=mint,
                                token_amount_raw=delta_raw,
                                sol_spent_lamports=int(sol_spent_lamports * fraction),
                                entry_price_sol_per_token=price,
                                block_time=block_time,
                                slot=slot,
                                swap_type="sol_to_token",
                                confidence="known",
                            )
                        )
                if events:
                    return events

    return []


def run_tx_first_lot_engine(
    state: RuntimeState,
    rpc: RpcClient,
    wallet_pubkey: str,
    decimals_by_mint: Dict[str, int],
    journal_path: Optional[Path] = None,
    max_signatures: int = 60,
    symbol_by_mint: Optional[Dict[str, str]] = None,
    delay_after_request_sec: float = 0.0,
) -> int:
    """
    Ingest recent wallet transactions, parse buys, create lots from tx data.
    Only creates lots for mints already in state.mints. Idempotent: skips
    (signature, mint) already present in state. Returns number of lots created.
    """
    mints_tracked = set(state.mints.keys())
    if not mints_tracked:
        return 0

    existing_sig_mint: Set[Tuple[str, str]] = set()
    for ms in state.mints.values():
        for lot in getattr(ms, "lots", None) or []:
            sig = getattr(lot, "tx_signature", None)
            if sig and getattr(lot, "mint", None):
                existing_sig_mint.add((sig, lot.mint))

    # Paginate: Solana returns max 1000 per request; fetch pages until we have max_signatures
    sig_list: List[Dict] = []
    page_limit = 1000
    before: Optional[str] = None
    page = 0
    try:
        while len(sig_list) < max_signatures:
            page += 1
            fetch_limit = min(page_limit, max_signatures - len(sig_list))
            batch = rpc.get_signatures_for_address(wallet_pubkey, limit=fetch_limit, before=before)
            if not batch:
                if page == 1:
                    logger.warning("LOT_SOURCE_TX_FIRST_STOP reason=no_signatures_returned (get_signatures_for_address returned 0 on first page)")
                break
            sig_list.extend(batch)
            logger.info("BUY_TX_INGESTED page=%d got=%d total_so_far=%d max_signatures=%d", page, len(batch), len(sig_list), max_signatures)
            if len(sig_list) >= max_signatures:
                break
            # Next page (older txs): need cursor; keep requesting until RPC returns empty
            before = batch[-1].get("signature") if isinstance(batch[-1], dict) else None
            if not before:
                logger.info("BUY_TX_INGESTED no cursor for next page (batch item may lack 'signature' key)")
                break
        logger.info("BUY_TX_INGESTED fetched %d signatures (max_signatures=%d)", len(sig_list), max_signatures)
    except Exception as exc:
        logger.warning("BUY_TX_INGESTED failed get_signatures_for_address: %s", exc)
        if journal_path:
            try:
                from .events import append_event, EVENT_TX_PARSE_FAILED
                append_event(journal_path, EVENT_TX_PARSE_FAILED, {"reason": "get_signatures_for_address", "error": str(exc)})
            except Exception:
                pass
        return 0

    lots_created = 0
    failures = 0

    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        if any(signature == s for s, _ in existing_sig_mint):
            logger.info("DUPLICATE_TX_LOT_SKIPPED sig=%s (tx already has lot for this mint)", signature[:16])
            if journal_path:
                try:
                    from .events import append_event, EVENT_TX_ALREADY_PROCESSED, EVENT_DUPLICATE_TX_LOT_SKIPPED
                    append_event(journal_path, EVENT_TX_ALREADY_PROCESSED, {"tx_sig": signature[:16]})
                    append_event(journal_path, EVENT_DUPLICATE_TX_LOT_SKIPPED, {"tx_sig": signature[:16]})
                except Exception:
                    pass
            continue

        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            logger.debug("get_transaction %s failed: %s", signature[:16], exc)
            failures += 1
            if journal_path and failures <= 3:
                try:
                    from .events import append_event, EVENT_TX_PARSE_FAILED
                    append_event(journal_path, EVENT_TX_PARSE_FAILED, {"tx_sig": signature[:16], "reason": "get_transaction", "error": str(exc)})
                except Exception:
                    pass
            if failures >= 3 or isinstance(exc, RpcError):
                stop_reason = "rpc_429" if "429" in str(exc).upper() else ("rpc_error" if isinstance(exc, RpcError) else "max_failures")
                logger.warning(
                    "LOT_SOURCE_TX_FIRST_STOP reason=%s failures=%d (remaining sigs not processed); check RPC rate limits",
                    stop_reason, failures,
                )
                break
            if delay_after_request_sec > 0:
                import time
                time.sleep(delay_after_request_sec)
            continue

        if delay_after_request_sec > 0:
            import time
            time.sleep(delay_after_request_sec)
        if not tx:
            continue

        events = _parse_buy_events_from_tx(
            tx, wallet_pubkey, signature, mints_tracked, decimals_by_mint
        )
        if not events:
            logger.debug("TX_NO_BUY_EVENTS sig=%s (no tracked mints received in this tx)", signature[:16])
            continue

        # Enrich token→token events: valuation = quote_value / token_amount only.
        # Allowed: sol_spent (SOL→token), source_lot_cost (FIFO), wsol_equivalent.
        # Never use market price as entry (would make all token→token lots identical).
        for ev in events:
            if ev.swap_type not in ("token_to_token", "multi_hop") or ev.entry_price_sol_per_token is not None:
                continue
            dec = decimals_by_mint.get(ev.mint, 6)
            token_human = ev.token_amount_raw / (10 ** dec) if ev.token_amount_raw and dec >= 0 else 0
            if token_human <= 0:
                continue
            # 1. source_lot_cost
            if ev.input_asset_mint and ev.input_amount_raw:
                res = _source_cost_basis_sol(state, ev.input_asset_mint, ev.input_amount_raw, decimals_by_mint)
                if res is not None:
                    cost_sol, method = res
                    ev.entry_price_sol_per_token = cost_sol / token_human
                    if _validate_entry_price(ev.entry_price_sol_per_token):
                        ev.confidence = "inferred"
                        ev.valuation_method = method
                        logger.info(
                            "TOKEN_TO_TOKEN_ENTRY_INFERRED dest_mint=%s source_mint=%s cost_sol=%.6e method=%s entry=%.6e",
                            ev.mint[:12], ev.input_asset_mint[:12], cost_sol, method, ev.entry_price_sol_per_token,
                        )
                        continue
            # 2. wsol_equivalent already set in _parse_buy_events_from_tx
            # 3–4. tx_market_* / enhanced_swap_amounts: optional (pool price at tx time)
            # Invariant: entry_price = quote_value / token_amount only. Never use market price
            # as entry for reconstruction (would make all token→token lots identical).
            if ev.entry_price_sol_per_token is None:
                logger.info(
                    "TOKEN_TO_TOKEN_ENTRY_UNKNOWN dest_mint=%s source_mint=%s reason=no_source_lot_cost_no_wsol (entry=null confidence=unknown)",
                    ev.mint[:12], (ev.input_asset_mint or "?")[:12],
                )

        if journal_path:
            try:
                from .events import append_event, EVENT_BUY_TX_INGESTED
                append_event(journal_path, EVENT_BUY_TX_INGESTED, {"tx_sig": signature[:16], "mints": [e.mint[:12] for e in events]})
            except Exception:
                pass

        for ev in events:
            if (ev.signature, ev.mint) in existing_sig_mint:
                logger.info("DUPLICATE_TX_LOT_SKIPPED mint=%s sig=%s (lot already exists)", ev.mint[:12], ev.signature[:16])
                if journal_path:
                    try:
                        from .events import append_event, EVENT_DUPLICATE_TX_LOT_SKIPPED
                        append_event(journal_path, EVENT_DUPLICATE_TX_LOT_SKIPPED, {"mint": ev.mint[:12], "tx_sig": ev.signature[:16]})
                    except Exception:
                        pass
                continue
            ms = state.mints.get(ev.mint)
            if not ms:
                continue
            # Allow lots with unknown entry (e.g. token→token without WSOL); reject only invalid numbers
            if ev.entry_price_sol_per_token is not None and not _validate_entry_price(ev.entry_price_sol_per_token):
                logger.warning(
                    "PRICE_SANITY_REJECTED tx_first mint=%s sig=%s price=%.6e",
                    ev.mint[:12], ev.signature[:16], ev.entry_price_sol_per_token,
                )
                if journal_path:
                    try:
                        from .events import append_event
                        append_event(journal_path, "PRICE_SANITY_REJECTED", {"mint": ev.mint[:12], "tx_sig": ev.signature[:16], "price": ev.entry_price_sol_per_token})
                    except Exception:
                        pass
                continue

            sym_map = symbol_by_mint or {}
            acquired_via_swap = ev.swap_type in ("token_to_token", "multi_hop")
            entry_conf = "unknown"
            if ev.entry_price_sol_per_token is not None:
                entry_conf = "exact" if ev.confidence == "known" else ev.confidence if ev.confidence in ("inferred", "bootstrap") else "inferred"
            # cost_basis_confidence allows only known|inferred|unknown; map bootstrap -> inferred
            cost_basis_conf = ev.confidence if ev.confidence in ("known", "inferred", "unknown") else "inferred"
            lot = LotInfo.create(
                mint=ev.mint,
                token_amount_raw=ev.token_amount_raw,
                entry_price=ev.entry_price_sol_per_token,
                confidence=cost_basis_conf,
                source="tx_exact" if ev.swap_type == "sol_to_token" else "tx_parsed",
                entry_confidence=entry_conf,
                tx_signature=ev.signature,
                detected_at=ev.block_time,
                swap_type=ev.swap_type,
                input_asset_mint=ev.input_asset_mint,
                input_asset_symbol=sym_map.get(ev.input_asset_mint) if ev.input_asset_mint else None,
                input_amount_raw=ev.input_amount_raw,
                output_asset_symbol=sym_map.get(ev.mint),
                entry_price_usd_per_token=ev.entry_price_usd_per_token,
                acquired_via_swap=acquired_via_swap,
                valuation_method=ev.valuation_method,
            )
            ms.lots = getattr(ms, "lots", None) or []
            ms.lots.append(lot)
            existing_sig_mint.add((ev.signature, ev.mint))
            lots_created += 1
            # Sync mint-level entry from token→token lot when valid (so status/dashboard show non-N/A)
            if ev.entry_price_sol_per_token is not None and _validate_entry_price(ev.entry_price_sol_per_token):
                current_ep = getattr(ms, "entry_price_sol_per_token", None) or 0
                if not _validate_entry_price(current_ep):
                    ms.entry_price_sol_per_token = ev.entry_price_sol_per_token
                    if getattr(ms, "original_entry_price_sol_per_token", None) is None or not _validate_entry_price(ms.original_entry_price_sol_per_token):
                        ms.original_entry_price_sol_per_token = ev.entry_price_sol_per_token
                    if getattr(ms, "working_entry_price_sol_per_token", None) is None or not _validate_entry_price(ms.working_entry_price_sol_per_token):
                        ms.working_entry_price_sol_per_token = ev.entry_price_sol_per_token
                    logger.info(
                        "TOKEN_TO_TOKEN_MINT_ENTRY_SYNCED mint=%s entry=%.6e from_lot",
                        ev.mint[:12], ev.entry_price_sol_per_token,
                    )

            if journal_path:
                try:
                    from .events import (
                        append_event,
                        EVENT_LOT_CREATED_FROM_TX,
                        LOT_ENTRY_SET_FROM_SOL_DELTA,
                        LOT_ENTRY_SET_FROM_WSOL_EQUIV,
                        LOT_ENTRY_SET_FROM_SOURCE_FIFO_COST,
                        LOT_ENTRY_LEFT_UNKNOWN,
                    )
                    append_event(journal_path, EVENT_LOT_CREATED_FROM_TX, {"mint": ev.mint[:12], "lot_id": lot.lot_id[:8], "tx_sig": ev.signature[:16], "entry_price": ev.entry_price_sol_per_token})
                    # Observability: which method set (or did not set) lot entry
                    payload = {"mint": ev.mint[:12], "source_mint": (ev.input_asset_mint or "")[:12] or None, "tx_sig": ev.signature[:16], "lot_id": lot.lot_id[:8], "entry": ev.entry_price_sol_per_token, "reason": "tx_first"}
                    if ev.swap_type == "sol_to_token" and ev.entry_price_sol_per_token is not None:
                        append_event(journal_path, LOT_ENTRY_SET_FROM_SOL_DELTA, {**payload, "method": "sol_delta"})
                    elif (ev.valuation_method or "") == "wsol_equivalent" and ev.entry_price_sol_per_token is not None:
                        append_event(journal_path, LOT_ENTRY_SET_FROM_WSOL_EQUIV, {**payload, "method": "wsol_equivalent"})
                    elif (ev.valuation_method or "").startswith("source_") and ev.entry_price_sol_per_token is not None:
                        append_event(journal_path, LOT_ENTRY_SET_FROM_SOURCE_FIFO_COST, {**payload, "method": ev.valuation_method or "source_lot_cost"})
                    elif ev.entry_price_sol_per_token is None:
                        append_event(journal_path, LOT_ENTRY_LEFT_UNKNOWN, {**payload, "method": "none", "reason": "no_source_lot_cost_no_wsol"})
                except Exception:
                    pass
            logger.info(
                "LOT_SOURCE_TX_DERIVED mint=%s lot_id=%s source=%s sig=%s price=%.6e",
                ev.mint[:12], lot.lot_id[:8], "tx_exact" if ev.swap_type == "sol_to_token" else "tx_parsed", ev.signature[:16], ev.entry_price_sol_per_token or 0,
            )

        # Token→token: debit source mint lots (once per tx per source mint); idempotent across restarts.
        processed = getattr(state, "processed_token_to_token_disposals", None)
        if processed is None:
            state.processed_token_to_token_disposals = []
            processed = state.processed_token_to_token_disposals
        debited_this_tx: Set[Tuple[str, str]] = set()
        for ev in events:
            if not ev.input_asset_mint or not ev.source_sold_raw or ev.swap_type not in ("token_to_token", "multi_hop"):
                continue
            disposal_key = ev.signature + "|" + ev.input_asset_mint
            if disposal_key in processed:
                logger.debug(
                    "TOKEN_TO_TOKEN_SOURCE_SKIP_IDEMPOTENT sig=%s source_mint=%s",
                    ev.signature[:16], ev.input_asset_mint[:12],
                )
                continue
            key = (ev.signature, ev.input_asset_mint)
            if key in debited_this_tx:
                continue
            source_ms = state.mints.get(ev.input_asset_mint)
            if not source_ms:
                continue
            _debit_lots_fifo_local(source_ms, ev.source_sold_raw)
            source_ms.trading_bag_raw = str(_trading_bag_from_lots_local(source_ms))
            debited_this_tx.add(key)
            processed.append(disposal_key)
            logger.info(
                "TOKEN_TO_TOKEN_SOURCE_DEBITED sig=%s source_mint=%s sold_raw=%s",
                ev.signature[:16], ev.input_asset_mint[:12], ev.source_sold_raw,
            )
            if journal_path:
                try:
                    from .events import append_event, EVENT_TOKEN_TO_TOKEN_SOURCE_DISPOSED
                    ts = ev.block_time.isoformat() if ev.block_time else None
                    append_event(
                        journal_path,
                        EVENT_TOKEN_TO_TOKEN_SOURCE_DISPOSED,
                        {
                            "sig": ev.signature[:22],
                            "source_mint": ev.input_asset_mint[:12],
                            "source_amount_raw": ev.source_sold_raw,
                            "destination_mint": ev.mint[:12],
                            "timestamp": ts,
                        },
                    )
                except Exception:
                    pass

    # No backfill of lot entry from market price: invariant is entry = quote_value/token_amount
    # only (SOL delta, source lot cost, or quote token converted via pool price). Market price
    # must never be used as entry for reconstruction.

    logger.info(
        "LOT_SOURCE_TX_FIRST_SUMMARY lots_created=%d signatures_fetched=%d",
        lots_created, len(sig_list),
    )
    return lots_created


def run_tx_first_lot_engine_from_sig_list(
    state: RuntimeState,
    rpc: RpcClient,
    wallet_pubkey: str,
    sorted_sig_list: List[Dict],
    decimals_by_mint: Dict[str, int],
    journal_path: Optional[Path] = None,
    symbol_by_mint: Optional[Dict[str, str]] = None,
    delay_after_request_sec: float = 0.0,
) -> int:
    """
    Same as run_tx_first_lot_engine but uses a pre-built sorted signature list
    (e.g. merged wallet + token-account history, sorted oldest-first).
    Used for full-history scratch rebuild. No pagination; processes sorted_sig_list in order.
    """
    mints_tracked = set(state.mints.keys())
    if not mints_tracked:
        return 0

    existing_sig_mint: Set[Tuple[str, str]] = set()
    for ms in state.mints.values():
        for lot in getattr(ms, "lots", None) or []:
            sig = getattr(lot, "tx_signature", None)
            if sig and getattr(lot, "mint", None):
                existing_sig_mint.add((sig, lot.mint))

    sig_list = sorted_sig_list
    logger.info("LOT_SOURCE_TX_FIRST_FROM_SIG_LIST processing %d signatures (oldest-first)", len(sig_list))

    lots_created = 0
    failures = 0

    for sig_info in sig_list:
        signature = sig_info.get("signature") if isinstance(sig_info, dict) else None
        if not signature:
            continue
        if any(signature == s for s, _ in existing_sig_mint):
            logger.debug("DUPLICATE_TX_LOT_SKIPPED sig=%s (tx already has lot for this mint)", signature[:16])
            continue

        try:
            tx = rpc.get_transaction(signature)
        except Exception as exc:
            logger.debug("get_transaction %s failed: %s", signature[:16], exc)
            failures += 1
            if journal_path and failures <= 3:
                try:
                    from .events import append_event, EVENT_TX_PARSE_FAILED
                    append_event(journal_path, EVENT_TX_PARSE_FAILED, {"tx_sig": signature[:16], "reason": "get_transaction", "error": str(exc)})
                except Exception:
                    pass
            if failures >= 3 or isinstance(exc, RpcError):
                stop_reason = "rpc_429" if "429" in str(exc).upper() else ("rpc_error" if isinstance(exc, RpcError) else "max_failures")
                logger.warning(
                    "LOT_SOURCE_TX_FIRST_STOP reason=%s failures=%d (remaining sigs not processed)",
                    stop_reason, failures,
                )
                break
            if delay_after_request_sec > 0:
                import time
                time.sleep(delay_after_request_sec)
            continue

        if delay_after_request_sec > 0:
            import time
            time.sleep(delay_after_request_sec)
        if not tx:
            continue

        events = _parse_buy_events_from_tx(
            tx, wallet_pubkey, signature, mints_tracked, decimals_by_mint
        )
        if not events:
            logger.debug("TX_NO_BUY_EVENTS sig=%s (no tracked mints received in this tx)", signature[:16])
            continue

        for ev in events:
            if ev.swap_type not in ("token_to_token", "multi_hop") or ev.entry_price_sol_per_token is not None:
                continue
            dec = decimals_by_mint.get(ev.mint, 6)
            token_human = ev.token_amount_raw / (10 ** dec) if ev.token_amount_raw and dec >= 0 else 0
            if token_human <= 0:
                continue
            if ev.input_asset_mint and ev.input_amount_raw:
                res = _source_cost_basis_sol(state, ev.input_asset_mint, ev.input_amount_raw, decimals_by_mint)
                if res is not None:
                    cost_sol, method = res
                    ev.entry_price_sol_per_token = cost_sol / token_human
                    if _validate_entry_price(ev.entry_price_sol_per_token):
                        ev.confidence = "inferred"
                        ev.valuation_method = method
                        continue
            if ev.entry_price_sol_per_token is None:
                logger.debug(
                    "TOKEN_TO_TOKEN_ENTRY_UNKNOWN dest_mint=%s source_mint=%s",
                    ev.mint[:12], (ev.input_asset_mint or "?")[:12],
                )

        if journal_path:
            try:
                from .events import append_event, EVENT_BUY_TX_INGESTED
                append_event(journal_path, EVENT_BUY_TX_INGESTED, {"tx_sig": signature[:16], "mints": [e.mint[:12] for e in events]})
            except Exception:
                pass

        for ev in events:
            if (ev.signature, ev.mint) in existing_sig_mint:
                continue
            ms = state.mints.get(ev.mint)
            if not ms:
                continue
            if ev.entry_price_sol_per_token is not None and not _validate_entry_price(ev.entry_price_sol_per_token):
                if journal_path:
                    try:
                        from .events import append_event
                        append_event(journal_path, "PRICE_SANITY_REJECTED", {"mint": ev.mint[:12], "tx_sig": ev.signature[:16], "price": ev.entry_price_sol_per_token})
                    except Exception:
                        pass
                continue

            sym_map = symbol_by_mint or {}
            acquired_via_swap = ev.swap_type in ("token_to_token", "multi_hop")
            entry_conf = "unknown"
            if ev.entry_price_sol_per_token is not None:
                entry_conf = "exact" if ev.confidence == "known" else ev.confidence if ev.confidence in ("inferred", "bootstrap") else "inferred"
            cost_basis_conf = ev.confidence if ev.confidence in ("known", "inferred", "unknown") else "inferred"
            lot = LotInfo.create(
                mint=ev.mint,
                token_amount_raw=ev.token_amount_raw,
                entry_price=ev.entry_price_sol_per_token,
                confidence=cost_basis_conf,
                source="tx_exact" if ev.swap_type == "sol_to_token" else "tx_parsed",
                entry_confidence=entry_conf,
                tx_signature=ev.signature,
                detected_at=ev.block_time,
                swap_type=ev.swap_type,
                input_asset_mint=ev.input_asset_mint,
                input_asset_symbol=sym_map.get(ev.input_asset_mint) if ev.input_asset_mint else None,
                input_amount_raw=ev.input_amount_raw,
                output_asset_symbol=sym_map.get(ev.mint),
                entry_price_usd_per_token=ev.entry_price_usd_per_token,
                acquired_via_swap=acquired_via_swap,
                valuation_method=ev.valuation_method,
            )
            ms.lots = getattr(ms, "lots", None) or []
            ms.lots.append(lot)
            existing_sig_mint.add((ev.signature, ev.mint))
            lots_created += 1
            if ev.entry_price_sol_per_token is not None and _validate_entry_price(ev.entry_price_sol_per_token):
                current_ep = getattr(ms, "entry_price_sol_per_token", None) or 0
                if not _validate_entry_price(current_ep):
                    ms.entry_price_sol_per_token = ev.entry_price_sol_per_token
                    if getattr(ms, "original_entry_price_sol_per_token", None) is None or not _validate_entry_price(ms.original_entry_price_sol_per_token):
                        ms.original_entry_price_sol_per_token = ev.entry_price_sol_per_token
                    if getattr(ms, "working_entry_price_sol_per_token", None) is None or not _validate_entry_price(ms.working_entry_price_sol_per_token):
                        ms.working_entry_price_sol_per_token = ev.entry_price_sol_per_token

            if journal_path:
                try:
                    from .events import append_event, EVENT_LOT_CREATED_FROM_TX
                    append_event(journal_path, EVENT_LOT_CREATED_FROM_TX, {"mint": ev.mint[:12], "lot_id": lot.lot_id[:8], "tx_sig": ev.signature[:16], "entry_price": ev.entry_price_sol_per_token})
                except Exception:
                    pass
            logger.info(
                "LOT_SOURCE_TX_DERIVED mint=%s lot_id=%s source=%s sig=%s price=%.6e",
                ev.mint[:12], lot.lot_id[:8], "tx_exact" if ev.swap_type == "sol_to_token" else "tx_parsed", ev.signature[:16], ev.entry_price_sol_per_token or 0,
            )

        processed = getattr(state, "processed_token_to_token_disposals", None)
        if processed is None:
            state.processed_token_to_token_disposals = []
            processed = state.processed_token_to_token_disposals
        debited_this_tx: Set[Tuple[str, str]] = set()
        for ev in events:
            if not ev.input_asset_mint or not ev.source_sold_raw or ev.swap_type not in ("token_to_token", "multi_hop"):
                continue
            disposal_key = ev.signature + "|" + ev.input_asset_mint
            if disposal_key in processed:
                continue
            key = (ev.signature, ev.input_asset_mint)
            if key in debited_this_tx:
                continue
            source_ms = state.mints.get(ev.input_asset_mint)
            if not source_ms:
                continue
            _debit_lots_fifo_local(source_ms, ev.source_sold_raw)
            source_ms.trading_bag_raw = str(_trading_bag_from_lots_local(source_ms))
            debited_this_tx.add(key)
            processed.append(disposal_key)
            logger.info(
                "TOKEN_TO_TOKEN_SOURCE_DEBITED sig=%s source_mint=%s sold_raw=%s",
                ev.signature[:16], ev.input_asset_mint[:12], ev.source_sold_raw,
            )
            if journal_path:
                try:
                    from .events import append_event, EVENT_TOKEN_TO_TOKEN_SOURCE_DISPOSED
                    ts = ev.block_time.isoformat() if ev.block_time else None
                    append_event(
                        journal_path,
                        EVENT_TOKEN_TO_TOKEN_SOURCE_DISPOSED,
                        {"sig": ev.signature[:22], "source_mint": ev.input_asset_mint[:12], "source_amount_raw": ev.source_sold_raw, "destination_mint": ev.mint[:12], "timestamp": ts},
                    )
                except Exception:
                    pass

    logger.info(
        "LOT_SOURCE_TX_FIRST_FROM_SIG_LIST_SUMMARY lots_created=%d signatures_processed=%d",
        lots_created, len(sig_list),
    )
    return lots_created
