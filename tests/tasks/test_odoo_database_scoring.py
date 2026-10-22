import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from ale_run.orchestration.judge_preflight import judge_requirements
from tasks.business_finance.odoo import shared
from tasks.utils import evaluation

REQUIRED_TABLES = [
    "product_template",
    "product_product",
    "product_category",
    "ir_model_fields",
    "ir_default",
    "res_currency",
    "res_currency_rate",
    "res_company",
    "res_partner",
    "stock_landed_cost",
    "stock_landed_cost_lines",
    "stock_valuation_adjustment_lines",
    "stock_warehouse",
    "stock_quant",
    "stock_lot",
    "stock_scrap",
    "stock_location",
    "stock_picking_type",
    "account_move",
    "account_move_line",
    "sale_order",
    "sale_order_line",
    "sale_order_line_invoice_rel",
    "purchase_order",
    "purchase_order_line",
    "mrp_production",
    "mrp_workorder",
    "mrp_bom",
    "mrp_bom_line",
    "mrp_routing_workcenter",
    "mrp_workcenter",
    "stock_picking",
    "stock_move_line",
    "stock_move",
    "account_analytic_line",
    "account_analytic_account",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment", ["valid", "missing", "corrupt"])
@pytest.mark.parametrize(
    "invoices,score",
    [
        (
            [
                {"amount": 230, "fp_qty": 1, "acc_qty": 1},
                {"amount": 460, "fp_qty": 2, "acc_qty": 2},
            ],
            1.0,
        ),
        ([], round(36 / 38, 4)),
    ],
)
async def test_database_score_does_not_depend_on_a_judge(monkeypatch, attachment, invoices, score):
    evidence = {
        "lc_split": {"RM-010": 60, "RM-020": 40},
        "lc_bill_paid_cnt": 1,
        "so": {"state": "sale", "fp_qty": 3, "acc_qty": 3, "customer_ok": True,
               "currency_name": "EUR", "prices_ok": True},
        "dropship_done_qty": 3,
        "mo_sa_done_qty": 3,
        "mo_fp_done_qty": 3,
        "workorders_done": True,
        "paid_invoices": invoices,
        "paid_credit": [200],
        "paid_purchase_requirements": [{"paid": True}] * 7,
        "fp_lots": ["SSK-0001", "SSK-0002", "SSK-0003"],
        "sc_lots": ["CS-0001", "CS-0002", "CS-0003"],
        "subcontract_sc_distinct_lots": 3,
        "subcontract_supply": True,
        "wh_workflow": True,
        "fp_handling": True,
        "scrap_rm030_cnt": 1,
        "extra_po_rm030_cnt": 1,
        "analytic_expense_cnt": 1,
        "wh_received": [{"product": "RM-010", "qty": 10}, {"product": "RM-020", "qty": 10}],
        "wh_qty": [
            {"wh": "My Co", "product": "RM-010", "qty": 4},
            {"wh": "My Co", "product": "RM-020", "qty": 7},
        ],
        "warehouses": [
            {"code": "WH", "reception_steps": "two_steps"},
            {"code": "My Co", "delivery_steps": "pick_pack_ship"},
        ],
        "products": ["RM-010", "RM-020"],
        "boms": {"fp": True, "sa": True, "sc": True, "assembly": True, "smt": True},
        "partial_delivery_and_invoicing": True,
        "currency_configuration": True,
        "inventory_valuation": True,
        "ssk2_wh": [{"wh": "My Co", "qty": 1}],
        "ssk2_return": True,
    }
    monkeypatch.setattr(shared, "_resolve_pg_paths", AsyncMock())
    monkeypatch.setattr(
        shared,
        "_run_psql_json",
        AsyncMock(
            side_effect=[
                dict.fromkeys(REQUIRED_TABLES, "table"),
                {},
                evidence,
            ]
        ),
    )
    judge = Mock(side_effect=AssertionError("Odoo must not call a judge"))
    monkeypatch.setattr(evaluation, "llm_judge_completion", judge)
    stream = BytesIO()
    Image.new("RGB", (2, 2)).save(stream, format="PNG")

    async def read_bytes(path):
        if attachment == "corrupt":
            return b"\xffbad"
        return stream.getvalue() if path.endswith(".png") else b"Operator submission"

    session = SimpleNamespace(
        interface=SimpleNamespace(create_dir=AsyncMock()),
        file_exists=AsyncMock(return_value=attachment != "missing"),
        read_bytes=AsyncMock(side_effect=read_bytes),
        write_file=AsyncMock(),
    )
    config = SimpleNamespace(metadata=shared.cfg_for_variant(shared.VARIANTS[0]).to_metadata())
    assert await shared.evaluate_variant_task(config, session) == [score]
    judge.assert_not_called()
    report = json.loads(session.write_file.call_args.args[1])
    assert report["scoring_basis"] == "database_checks"
    assert len(report["score_items"]) == 38
    assert len(report["delivery_checks"]) == 5
    assert all(
        check["readable"] == (attachment == "valid") for check in report["delivery_checks"].values()
    )


def test_odoo_does_not_require_judge_credentials():
    assert judge_requirements(["business_finance/odoo"]) == {}


@pytest.mark.asyncio
async def test_evidence_query_failure_is_not_a_solver_zero(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_pg_paths", AsyncMock())
    monkeypatch.setattr(
        shared,
        "_run_psql_json",
        AsyncMock(
            side_effect=[
                dict.fromkeys(REQUIRED_TABLES, "table"),
                {},
                RuntimeError("SQL failure"),
            ]
        ),
    )
    session = SimpleNamespace(
        interface=SimpleNamespace(create_dir=AsyncMock()), write_file=AsyncMock()
    )
    config = SimpleNamespace(metadata=shared.cfg_for_variant(shared.VARIANTS[0]).to_metadata())
    with pytest.raises(RuntimeError, match="Odoo database evidence query failed"):
        await shared.evaluate_variant_task(config, session)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,expected", [(shared.PsqlCommandError(2, "database missing"), 0.0),
                                             (shared.PsqlCommandError(3, "invalid SQL"), None),
                                             (RuntimeError("transport failed"), None)])
async def test_database_preflight_distinguishes_missing_db_from_transport_failure(monkeypatch, error, expected):
    monkeypatch.setattr(shared, "_resolve_pg_paths", AsyncMock())
    monkeypatch.setattr(shared, "_run_psql_json", AsyncMock(side_effect=error))
    session = SimpleNamespace(interface=SimpleNamespace(create_dir=AsyncMock()), write_file=AsyncMock())
    config = SimpleNamespace(metadata=shared.cfg_for_variant(shared.VARIANTS[0]).to_metadata())
    if expected is None:
        with pytest.raises(RuntimeError, match="Odoo database preflight failed"):
            await shared.evaluate_variant_task(config, session)
    else:
        assert await shared.evaluate_variant_task(config, session) == [expected]


@pytest.mark.asyncio
async def test_psql_exit_code_is_propagated_through_powershell():
    session = SimpleNamespace(
        write_file=AsyncMock(),
        run_command=AsyncMock(return_value={"return_code": 0, "stdout": "__ALE_PSQL_STATUS__=1", "stderr": "database missing"}),
    )
    with pytest.raises(shared.PsqlCommandError, match="database missing"):
        await shared._run_psql_json(session, "psql", "", "missing", "SELECT 1", r"C:\temp", "pw")
    command = session.run_command.call_args.args[0]
    assert "ON_ERROR_STOP=1" in command
    assert "$psql_status=$LASTEXITCODE" in command


@pytest.mark.asyncio
async def test_psql_requires_a_reported_status():
    session = SimpleNamespace(write_file=AsyncMock(), run_command=AsyncMock(return_value={"stdout": ""}))
    with pytest.raises(RuntimeError, match="psql status not reported"):
        await shared._run_psql_json(session, "psql", "", "test", "SELECT 1", r"C:\temp", None)


@pytest.mark.asyncio
async def test_missing_required_module_columns_are_a_solver_failure(monkeypatch):
    monkeypatch.setattr(shared, "_resolve_pg_paths", AsyncMock())
    monkeypatch.setattr(
        shared,
        "_run_psql_json",
        AsyncMock(
            side_effect=[
                dict.fromkeys(REQUIRED_TABLES, "table"),
                {"module_columns_ok": False},
            ]
        ),
    )
    session = SimpleNamespace(
        interface=SimpleNamespace(create_dir=AsyncMock()), write_file=AsyncMock()
    )
    config = SimpleNamespace(metadata=shared.cfg_for_variant(shared.VARIANTS[0]).to_metadata())
    assert await shared.evaluate_variant_task(config, session) == [0.0]
