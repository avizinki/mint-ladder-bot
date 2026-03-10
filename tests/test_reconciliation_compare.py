"""
Step 4: Before/after reconciliation comparison tests.

Deterministic; no state mutation.
"""
from __future__ import annotations

from mint_ladder_bot.transfer_provenance_scratch import (
    PROPOSED_TRUSTED_TRANSFER_DERIVED,
    REMAINS_UNTRUSTED_OR_NONTRADABLE,
    SRC_RECON_SUCCESS,
    ScratchCandidateReport,
    _meaningful_improvement_rule,
    build_reconciliation_comparison_report,
)


def _report(
    proposed_classification: str,
    amount_propagated_raw: int,
    proposed_entry: float | None,
    before_sum: int,
    after_sum: int,
    wallet_balance: int,
    diff_pct_before: float | None,
    diff_pct_after: float | None,
    status_before: str,
    status_after: str,
) -> ScratchCandidateReport:
    return ScratchCandidateReport(
        destination_wallet="Dest",
        source_wallet="Src",
        mint="Mint1",
        symbol="TKN",
        tx_signature="sig1",
        transferred_amount_raw=500,
        source_reconstruction_status=SRC_RECON_SUCCESS,
        source_provenance_valid_available_raw=500,
        amount_propagated_raw=amount_propagated_raw,
        proposed_classification=proposed_classification,
        proposed_entry_price_sol_per_token=proposed_entry,
        why="test",
        before={
            "lot_count": 0,
            "sum_active_lots_raw": before_sum,
            "wallet_balance_raw": wallet_balance,
            "diff_pct": diff_pct_before,
            "reconciliation_status": status_before,
            "blocker_category": "other",
        },
        after={
            "lot_count": 1,
            "sum_active_lots_raw": after_sum,
            "wallet_balance_raw": wallet_balance,
            "diff_pct": diff_pct_after,
            "reconciliation_status": status_after,
            "blocker_category": "other",
        },
        improvement_meaningful=False,
        improvement_numerical_only=False,
        satisfies_strict_safety_rule=True,
        proposed_lot_in_memory=None,
    )


def test_meaningful_improvement_when_all_conditions_met():
    """Before/after comparison where propagation meaningfully improves reconciliation."""
    report = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=500,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=500,
        wallet_balance=500,
        diff_pct_before=1.0,
        diff_pct_after=0.0,
        status_before="insufficient",
        status_after="sufficient",
    )
    comp = build_reconciliation_comparison_report(report)
    assert comp["improvement_assessment"]["improvement_numerical"] is True
    assert comp["improvement_assessment"]["improvement_meaningful"] is True
    assert "identity" in comp
    assert comp["before"]["sum_active_lots_raw_before"] == 0
    assert comp["after"]["sum_active_lots_raw_after"] == 500
    assert comp["transfer_result"]["proposed_classification"] == PROPOSED_TRUSTED_TRANSFER_DERIVED


def test_rejected_no_improvement():
    """Before/after where propagation is rejected and no improvement occurs."""
    report = _report(
        proposed_classification=REMAINS_UNTRUSTED_OR_NONTRADABLE,
        amount_propagated_raw=0,
        proposed_entry=None,
        before_sum=0,
        after_sum=0,
        wallet_balance=500,
        diff_pct_before=1.0,
        diff_pct_after=1.0,
        status_before="insufficient",
        status_after="insufficient",
    )
    comp = build_reconciliation_comparison_report(report)
    assert comp["improvement_assessment"]["improvement_meaningful"] is False
    assert comp["improvement_assessment"]["improvement_numerical"] is False
    assert comp["transfer_result"]["proposed_classification"] == REMAINS_UNTRUSTED_OR_NONTRADABLE


def test_zero_propagated_never_meaningful():
    """Zero propagated amount never counts as meaningful improvement."""
    is_meaningful, explanation = _meaningful_improvement_rule(
        _report(
            proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
            amount_propagated_raw=0,
            proposed_entry=1e-6,
            before_sum=0,
            after_sum=0,
            wallet_balance=500,
            diff_pct_before=1.0,
            diff_pct_after=0.0,
            status_before="insufficient",
            status_after="sufficient",
        )
    )
    assert is_meaningful is False
    assert "zero" in explanation.lower()


def test_same_input_same_comparison_output():
    """Same inputs -> same comparison output (deterministic)."""
    report = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=100,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=100,
        wallet_balance=100,
        diff_pct_before=0.5,
        diff_pct_after=0.0,
        status_before="partial",
        status_after="sufficient",
    )
    c1 = build_reconciliation_comparison_report(report)
    c2 = build_reconciliation_comparison_report(report)
    assert c1["identity"]["tx_signature"] == c2["identity"]["tx_signature"]
    assert c1["improvement_assessment"]["improvement_meaningful"] == c2["improvement_assessment"]["improvement_meaningful"]
    assert c1["before"]["sum_active_lots_raw_before"] == c2["before"]["sum_active_lots_raw_before"]


def test_multiple_candidates_stable_ordering():
    """Multiple candidates produce stable output ordering."""
    r1 = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=100,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=100,
        wallet_balance=100,
        diff_pct_before=0.5,
        diff_pct_after=0.0,
        status_before="partial",
        status_after="sufficient",
    )
    r2 = _report(
        proposed_classification=REMAINS_UNTRUSTED_OR_NONTRADABLE,
        amount_propagated_raw=0,
        proposed_entry=None,
        before_sum=0,
        after_sum=0,
        wallet_balance=200,
        diff_pct_before=1.0,
        diff_pct_after=1.0,
        status_before="insufficient",
        status_after="insufficient",
    )
    comps = [build_reconciliation_comparison_report(r1), build_reconciliation_comparison_report(r2)]
    assert len(comps) == 2
    assert comps[0]["identity"]["mint"] == comps[0]["identity"]["mint"]
    assert comps[0]["improvement_assessment"]["improvement_meaningful"] is True
    assert comps[1]["improvement_assessment"]["improvement_meaningful"] is False


def test_comparison_structure_has_required_sections():
    """Output has IDENTITY, BEFORE, AFTER, TRANSFER_RESULT, IMPROVEMENT_ASSESSMENT."""
    report = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=50,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=50,
        wallet_balance=50,
        diff_pct_before=0.2,
        diff_pct_after=0.0,
        status_before="partial",
        status_after="sufficient",
    )
    comp = build_reconciliation_comparison_report(report)
    assert "identity" in comp
    assert "destination_wallet" in comp["identity"]
    assert "source_wallet" in comp["identity"]
    assert "mint" in comp["identity"]
    assert "tx_signature" in comp["identity"]
    assert "transferred_amount_raw" in comp["identity"]
    assert "before" in comp
    assert "lot_count_before" in comp["before"]
    assert "sum_active_lots_raw_before" in comp["before"]
    assert "diff_pct_before" in comp["before"]
    assert "reconciliation_status_before" in comp["before"]
    assert "blocker_category_before" in comp["before"]
    assert "after" in comp
    assert "lot_count_after" in comp["after"]
    assert "sum_active_lots_raw_after" in comp["after"]
    assert "transfer_result" in comp
    assert "proposed_classification" in comp["transfer_result"]
    assert "amount_propagated_raw" in comp["transfer_result"]
    assert "why_accepted_or_rejected" in comp["transfer_result"]
    assert "improvement_assessment" in comp
    assert "improvement_numerical" in comp["improvement_assessment"]
    assert "improvement_meaningful" in comp["improvement_assessment"]
    assert "explanation" in comp["improvement_assessment"]


def test_no_status_or_diff_improvement_not_meaningful():
    """When status and diff_pct do not improve, not meaningful."""
    report = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=100,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=100,
        wallet_balance=100,
        diff_pct_before=0.1,
        diff_pct_after=0.1,
        status_before="sufficient",
        status_after="sufficient",
    )
    is_meaningful, explanation = _meaningful_improvement_rule(report)
    assert is_meaningful is False
    assert "did not improve" in explanation or "not materially" in explanation


def test_after_exceeds_balance_not_meaningful():
    """When after sum exceeds wallet balance, not safety-valid -> not meaningful."""
    report = _report(
        proposed_classification=PROPOSED_TRUSTED_TRANSFER_DERIVED,
        amount_propagated_raw=600,
        proposed_entry=1e-6,
        before_sum=0,
        after_sum=600,
        wallet_balance=500,
        diff_pct_before=1.0,
        diff_pct_after=0.0,
        status_before="insufficient",
        status_after="sufficient",
    )
    is_meaningful, explanation = _meaningful_improvement_rule(report)
    assert is_meaningful is False
    assert "exceeds" in explanation or "safety" in explanation.lower()
