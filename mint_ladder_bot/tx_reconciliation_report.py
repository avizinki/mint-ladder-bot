from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .data.helius_client import get_wallet_transactions
from .dashboard_truth import token_truth
from .models import RuntimeState, StatusFile
from .rpc import RpcClient
from .tx_infer import parse_sell_events_from_tx


@dataclass
class TxReconciliationRow:
    tx_signature: str
    timestamp: Optional[str]
    mint_in: Optional[str]
    mint_out: Optional[str]
    token_delta: Dict[str, int]
    sol_delta: float
    classification: str
    bot_detected: bool
    accounting_updated: bool
    dashboard_reflected: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tx_signature": self.tx_signature,
            "timestamp": self.timestamp,
            "mint_in": self.mint_in,
            "mint_out": self.mint_out,
            "token_delta": self.token_delta,
            "sol_delta": self.sol_delta,
            "classification": self.classification,
            "bot_detected": self.bot_detected,
            "accounting_updated": self.accounting_updated,
            "dashboard_reflected": self.dashboard_reflected,
        }


def build_tx_reconciliation_report(
    wallet: str,
    state: RuntimeState,
    status: StatusFile,
    rpc: RpcClient,
    limit: int = 50,
) -> List[TxReconciliationRow]:
    """
    Build a lightweight, read-only reconciliation report for the last N wallet txs.

    This is diagnostics-only: does not mutate runtime state or accounting.
    """
    txs = get_wallet_transactions(wallet, limit=limit)
    rows: List[TxReconciliationRow] = []
    mints_tracked = set(state.mints.keys())
    status_by_mint = {m.mint: m.model_dump() for m in status.mints}

    for t in txs:
        sig = t.get("signature") or ""
        ts_raw = t.get("timestamp")
        ts = None
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc).isoformat()

        # Use tx_infer classification where possible.
        try:
            tx_full = rpc.get_transaction(sig)
        except Exception:
            tx_full = None

        events = parse_sell_events_from_tx(tx_full, wallet, mints_tracked, sig) if tx_full else []

        token_delta: Dict[str, int] = {}
        sol_delta = 0.0
        mint_in: Optional[str] = None
        mint_out: Optional[str] = None
        classification = "ignored"

        if events:
            # External-style sells: token out, SOL in.
            for ev in events:
                token_delta[ev.mint] = token_delta.get(ev.mint, 0) - int(ev.sold_raw)
                sol_delta += ev.sol_in_lamports / 1e9
                mint_out = ev.mint
            classification = "external_sell"
        else:
            classification = "unknown"

        # Approximate whether bot has seen this tx via executed_steps.
        bot_detected = False
        accounting_updated = False
        dashboard_reflected = False

        for mint_addr, ms in state.mints.items():
            for step in (getattr(ms, "executed_steps", None) or {}).values():
                if getattr(step, "sig", None) == sig:
                    bot_detected = True
                    accounting_updated = True
                    break
            if bot_detected:
                break

        # Dashboard reflection: reuse token_truth in a shallow way.
        for mint_addr, ms in state.mints.items():
            sm = status_by_mint.get(mint_addr) or {}
            sold_raw = sum(int(getattr(s, "sold_raw", 0) or 0) for s in (ms.executed_steps or {}).values())
            truth = token_truth(
                mint_addr,
                ms.model_dump(),
                sm,
                decimals=getattr(next((m for m in status.mints if m.mint == mint_addr), None), "decimals", 6),
                symbol=next((m.symbol or m.mint[:8] for m in status.mints if m.mint == mint_addr), mint_addr[:8]),
                sold_raw_from_steps=sold_raw,
            )
            if sold_raw:
                dashboard_reflected = True
                break

        rows.append(
            TxReconciliationRow(
                tx_signature=sig,
                timestamp=ts,
                mint_in=mint_in,
                mint_out=mint_out,
                token_delta=token_delta,
                sol_delta=sol_delta,
                classification=classification,
                bot_detected=bot_detected,
                accounting_updated=accounting_updated,
                dashboard_reflected=dashboard_reflected,
            )
        )

    return rows

