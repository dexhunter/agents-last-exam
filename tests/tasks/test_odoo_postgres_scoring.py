import json
import os
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tasks.business_finance.odoo import shared


CONTAINER = os.getenv("ALE_ODOO_TEST_POSTGRES_CONTAINER", "")
pytestmark = pytest.mark.skipif(
    not CONTAINER, reason="requires a disposable PostgreSQL test container"
)

SCHEMA = """
DROP SCHEMA public CASCADE; CREATE SCHEMA public;
CREATE TABLE product_template (id int, default_code text);
CREATE TABLE product_product (id int, product_tmpl_id int);
CREATE TABLE res_currency (id int, name text);
CREATE TABLE res_currency_rate (currency_id int, company_id int, name date, rate numeric);
CREATE TABLE res_company (id int, currency_id int, cost_method text, inventory_valuation text);
CREATE TABLE product_category (id int, property_cost_method jsonb, property_valuation jsonb);
CREATE TABLE ir_model_fields (id int, model text, name text);
CREATE TABLE ir_default (id int, field_id int, user_id int, company_id int, condition text, json_value text);
CREATE TABLE res_partner (id int, ref text);
CREATE TABLE sale_order (id int, name text, client_order_ref text, origin text, note text, state text);
CREATE TABLE sale_order_line (id int, order_id int, product_id int, product_uom_qty numeric);
CREATE TABLE purchase_order (id int, name text, partner_ref text, origin text, partner_id int, currency_id int, state text);
CREATE TABLE purchase_order_line (id int, order_id int, product_id int, product_qty numeric, price_unit numeric, sale_line_id int);
CREATE TABLE account_move (id int, currency_id int, move_type text, state text, payment_state text, amount_total numeric, ref text, invoice_origin text, narration text);
CREATE TABLE account_move_line (id int, move_id int, product_id int, quantity numeric, purchase_line_id int);
CREATE TABLE sale_order_line_invoice_rel (order_line_id int, invoice_line_id int);
CREATE TABLE stock_location (id int, usage text, parent_path text);
CREATE TABLE stock_warehouse (id int, code text, lot_stock_id int, reception_steps text, delivery_steps text);
CREATE TABLE stock_picking (id int, picking_type_id int, state text, sale_id int);
CREATE TABLE stock_picking_type (id int, code text, warehouse_id int);
CREATE TABLE stock_move (id int, sale_line_id int, purchase_line_id int);
CREATE TABLE stock_move_line (id int, picking_id int, move_id int, product_id int, location_id int, location_dest_id int, quantity numeric, lot_id int);
CREATE TABLE stock_quant (product_id int, location_id int, quantity numeric, lot_id int);
CREATE TABLE stock_lot (id int, product_id int, name text);
CREATE TABLE stock_scrap (product_id int, state text, scrap_qty numeric);
CREATE TABLE mrp_production (id int, name text, product_id int, state text, product_qty numeric, origin text, bom_id int);
CREATE TABLE mrp_workorder (id int, production_id int, state text);
CREATE TABLE mrp_bom (id int, product_tmpl_id int, product_qty numeric, type text, active boolean);
CREATE TABLE mrp_bom_line (bom_id int, product_id int, product_qty numeric);
CREATE TABLE mrp_routing_workcenter (bom_id int, name text, workcenter_id int, time_cycle_manual numeric);
CREATE TABLE mrp_workcenter (id int, costs_hour numeric);
CREATE TABLE stock_landed_cost (id int, name text, description text, state text, vendor_bill_id int);
CREATE TABLE stock_landed_cost_lines (id int, product_id int, price_unit numeric, split_method text);
CREATE TABLE stock_valuation_adjustment_lines (cost_id int, product_id int, additional_landed_cost numeric);
CREATE TABLE account_analytic_account (id int, name text);
CREATE TABLE account_analytic_line (id int, account_id int, name text, amount numeric);
CREATE TABLE hr_expense (id int, name text, currency_id int, state text, approval_state text, account_move_id int, total_amount_currency numeric);

INSERT INTO product_template VALUES
 (1,'FP-1000'),(2,'SA-200'),(3,'SC-300'),(4,'RM-010'),(5,'RM-020'),
 (6,'RM-030'),(7,'RM-040'),(8,'ACC-900'),(9,'FREIGHT-100');
ALTER TABLE product_template ADD COLUMN is_storable boolean, ADD COLUMN categ_id int;
UPDATE product_template SET is_storable=true, categ_id=1;
INSERT INTO product_product SELECT id, id FROM product_template;
INSERT INTO res_currency VALUES (1,'EUR'),(2,'USD'),(3,'CNY');
ALTER TABLE res_currency ADD COLUMN active boolean DEFAULT true;
INSERT INTO res_currency_rate VALUES (1,NULL,'2020-01-01',1.0/1.2),(3,NULL,'2020-01-01',8);
INSERT INTO res_company VALUES (1,2,'standard','periodic');
INSERT INTO product_category VALUES (1,'{"1":"fifo"}','{"1":"real_time"}');
INSERT INTO ir_model_fields VALUES (1,'product.category','property_cost_method'),(2,'product.category','property_valuation');
INSERT INTO res_partner VALUES (1,'V-CN01'),(2,'V-US01'),(3,'V-US02'),(4,'V-SUB01'),(5,'V-DS01'),(6,'CUST-EU01');
ALTER TABLE res_partner ADD COLUMN commercial_partner_id int;
ALTER TABLE res_partner ADD COLUMN name text;
INSERT INTO sale_order VALUES (1,'S00027','odoo_compact','','','sale'),(2,'OTHER','','','','sale');
ALTER TABLE sale_order ADD COLUMN company_id int DEFAULT 1, ADD COLUMN partner_id int DEFAULT 6, ADD COLUMN currency_id int DEFAULT 1;
INSERT INTO sale_order_line VALUES (1,1,1,3),(2,1,8,3),(3,2,8,3);
ALTER TABLE sale_order_line ADD COLUMN price_unit numeric;
UPDATE sale_order_line SET price_unit=CASE WHEN product_id=1 THEN 200 ELSE 30 END;
INSERT INTO purchase_order VALUES
 (1,'PO1','odoo_compact','',1,3,'purchase'),(2,'PO2','odoo_compact','',2,2,'purchase'),
 (3,'PO3','odoo_compact','',3,2,'purchase'),(4,'PO4','odoo_compact','',4,2,'purchase'),
 (5,'PO5','odoo_compact','',5,2,'purchase'),(6,'PO6','odoo_compact','',2,2,'purchase');
INSERT INTO purchase_order_line VALUES
 (1,1,4,10,96,NULL),(2,1,5,10,64,NULL),(3,2,6,3,20,NULL),
 (4,3,7,5,7,NULL),(5,4,3,3,25,NULL),(6,5,8,3,10,2),(7,6,6,1,20,NULL);
INSERT INTO account_move VALUES
 (1,2,'in_invoice','posted','paid',100,'','',''),
 (2,3,'in_invoice','posted','paid',1600,'','',''),
 (3,2,'in_invoice','posted','paid',60,'','',''),
 (4,2,'in_invoice','posted','paid',35,'','',''),
 (5,2,'in_invoice','posted','paid',75,'','',''),
 (6,2,'in_invoice','posted','paid',30,'','',''),
 (7,2,'in_invoice','posted','paid',20,'','',''),
 (8,1,'out_invoice','posted','paid',460,'','',''),
 (9,1,'out_invoice','posted','paid',230,'','',''),
 (10,1,'out_refund','posted','paid',200,'odoo_compact','',''),
 (11,2,'in_invoice','posted','paid',123.45,'','','');
ALTER TABLE account_move ADD COLUMN create_date timestamp;
UPDATE account_move SET create_date='2026-01-01 10:05' WHERE id=8;
UPDATE account_move SET create_date='2026-01-01 10:15' WHERE id=9;
INSERT INTO account_move_line VALUES
 (1,2,4,10,1),(2,2,5,10,2),(3,3,6,3,3),(4,4,7,5,4),
 (5,5,3,3,5),(6,6,8,3,6),(7,7,6,1,7),
 (8,8,1,2,NULL),(9,8,8,2,NULL),(10,9,1,1,NULL),(11,9,8,1,NULL);
INSERT INTO account_move_line VALUES (12,1,9,1,NULL);
INSERT INTO account_move_line VALUES (13,10,1,1,NULL);
INSERT INTO account_move_line VALUES (14,11,2,1,NULL);
ALTER TABLE account_move_line ADD COLUMN price_subtotal numeric;
UPDATE account_move_line SET price_subtotal=100 WHERE id=12;
UPDATE account_move_line SET price_subtotal=200 WHERE id=13;
ALTER TABLE account_move_line ADD COLUMN price_unit numeric;
UPDATE account_move_line line SET price_unit=pol.price_unit FROM purchase_order_line pol WHERE line.purchase_line_id=pol.id;
UPDATE account_move SET invoice_origin='S00027' WHERE id=10;
INSERT INTO sale_order_line_invoice_rel VALUES (1,8),(2,9),(1,10),(2,11);
INSERT INTO stock_location VALUES (1,'supplier','1/'),(2,'customer','2/'),(3,'internal','3/'),(4,'internal','4/');
INSERT INTO stock_warehouse VALUES (1,'WH',3,'two_steps','ship_only'),(2,'My Co',4,'one_step','pick_pack_ship');
ALTER TABLE stock_warehouse ADD COLUMN wh_input_stock_loc_id int,
 ADD COLUMN wh_pack_stock_loc_id int, ADD COLUMN wh_output_stock_loc_id int;
UPDATE stock_warehouse SET wh_input_stock_loc_id=10 WHERE code='WH';
UPDATE stock_warehouse SET wh_pack_stock_loc_id=11, wh_output_stock_loc_id=12 WHERE code='My Co';
INSERT INTO stock_location VALUES (10,'internal','10/'),(11,'internal','11/'),
 (12,'internal','12/'),(13,'internal','13/');
INSERT INTO stock_picking_type VALUES (1,'dropship',NULL),(2,'incoming',NULL),(3,'outgoing',2);
INSERT INTO stock_picking VALUES (1,1,'done',1),(2,1,'done',1),(3,2,'done',NULL),(4,1,'done',2);
INSERT INTO stock_picking VALUES (5,3,'done',1),(6,3,'done',1);
ALTER TABLE stock_picking ADD COLUMN date_done timestamp, ADD COLUMN backorder_id int;
UPDATE stock_picking SET date_done='2026-01-01 10:00' WHERE id=5;
UPDATE stock_picking SET date_done='2026-01-01 10:10', backorder_id=5 WHERE id=6;
INSERT INTO stock_move VALUES (1,2,6),(2,2,6),(3,NULL,5),(4,3,NULL),(5,1,NULL),(6,1,NULL);
ALTER TABLE stock_move ADD COLUMN origin_returned_move_id int;
INSERT INTO stock_move VALUES (7,NULL,NULL,5),(8,NULL,1,NULL),(9,NULL,2,NULL);
ALTER TABLE stock_picking ADD COLUMN origin text;
INSERT INTO stock_picking (id,picking_type_id,state,sale_id,origin) VALUES (7,2,'done',NULL,'odoo_compact return');
INSERT INTO stock_move_line VALUES
 (1,1,1,8,1,2,2,NULL),(2,2,2,8,1,2,1,NULL),
 (3,3,3,3,1,4,1,4),(4,3,3,3,1,4,1,5),(5,3,3,3,1,4,1,6),
 (6,4,4,8,1,2,3,NULL),(10,5,5,1,4,2,1,1),(11,5,5,1,4,2,1,2),(12,6,6,1,4,2,1,3),
 (13,7,7,1,2,4,1,2);
ALTER TABLE stock_move ADD COLUMN picking_id int;
ALTER TABLE stock_move ADD COLUMN state text DEFAULT 'done';
ALTER TABLE stock_move ADD COLUMN origin text;
ALTER TABLE stock_move ADD COLUMN location_id int;
UPDATE stock_move SET location_id=13 WHERE id=3;
UPDATE stock_move move SET picking_id=line.picking_id FROM stock_move_line line WHERE line.move_id=move.id;
INSERT INTO stock_picking_type VALUES (4,'incoming',1);
INSERT INTO stock_picking (id,picking_type_id,state,sale_id,origin) VALUES (20,4,'done',NULL,'PO1');
INSERT INTO stock_move (id,purchase_line_id,picking_id) VALUES (20,1,20),(21,2,20);
INSERT INTO stock_move_line VALUES (20,20,20,4,1,3,10,NULL),(21,20,21,5,1,3,10,NULL);
UPDATE stock_move_line SET location_dest_id=10 WHERE id IN (20,21);
UPDATE stock_move_line SET location_id=13 WHERE id IN (3,4,5);
INSERT INTO stock_picking_type VALUES (5,'internal',1),(6,'internal',2);
INSERT INTO stock_picking (id,picking_type_id,state,sale_id,origin) VALUES
 (30,5,'done',NULL,'PO1'),(31,5,'done',NULL,'odoo_compact'),
 (32,6,'done',1,'S00027'),(33,6,'done',1,'S00027'),(34,6,'done',NULL,'PO4');
INSERT INTO stock_move (id,sale_line_id,picking_id,origin) VALUES
 (30,NULL,30,'PO1'),(31,NULL,30,'PO1'),(32,NULL,31,NULL),(33,NULL,31,NULL),
 (34,1,32,'S00027'),(35,1,33,'S00027'),(36,NULL,34,'PO4');
INSERT INTO stock_move_line VALUES
 (30,30,30,4,10,3,10,NULL),(31,30,31,5,10,3,10,NULL),
 (32,31,32,4,3,4,10,NULL),(33,31,33,5,3,4,10,NULL),
 (34,32,34,1,4,11,1,1),(35,32,34,1,4,11,1,2),(36,32,34,1,4,11,1,3),
 (37,33,35,1,11,12,1,1),(38,33,35,1,11,12,1,2),(39,33,35,1,11,12,1,3),
 (40,34,36,7,4,13,3,NULL);
INSERT INTO stock_lot VALUES
 (1,1,'SSK-0001'),(2,1,'SSK-0002'),(3,1,'SSK-0003'),
 (4,3,'CS-0001'),(5,3,'CS-0002'),(6,3,'CS-0003');
INSERT INTO stock_quant VALUES (4,4,4,NULL),(5,4,7,NULL),(1,4,1,2);
INSERT INTO stock_scrap VALUES (6,'done',1);
INSERT INTO mrp_production VALUES (1,'MO-FP',1,'done',3,'S00027',1),(2,'MO-SA',2,'done',3,'odoo_compact',2);
INSERT INTO mrp_workorder VALUES (1,1,'done'),(2,2,'done'),(3,999,'done');
INSERT INTO mrp_bom VALUES (1,1,1,'normal',true),(2,2,1,'normal',true),(3,3,1,'subcontract',true);
INSERT INTO mrp_bom_line VALUES (1,2,1),(1,3,1),(1,4,2),(2,5,1),(2,6,1),(3,7,1);
INSERT INTO mrp_workcenter VALUES (1,40),(2,60);
INSERT INTO mrp_routing_workcenter VALUES (1,'Assembly',1,15),(2,'SMT',2,30);
INSERT INTO stock_landed_cost VALUES (1,'odoo_compact','','done',1);
INSERT INTO stock_landed_cost_lines VALUES (1,9,100,'by_current_cost_price');
INSERT INTO stock_valuation_adjustment_lines VALUES (1,4,60),(1,5,40);
ALTER TABLE stock_valuation_adjustment_lines ADD COLUMN cost_line_id int, ADD COLUMN move_id int;
UPDATE stock_valuation_adjustment_lines SET cost_line_id=1, move_id=CASE WHEN product_id=4 THEN 8 ELSE 9 END;
INSERT INTO account_analytic_account VALUES (1,'AN-odoo_compact');
INSERT INTO account_analytic_line VALUES (1,1,'Travel to Berlin',-123.45);
ALTER TABLE account_analytic_line ADD COLUMN move_line_id int;
UPDATE account_analytic_line SET move_line_id=14;
INSERT INTO hr_expense VALUES (1,'Travel to Berlin',2,'paid','approved',11,123.45);
"""


def sql(statement):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", CONTAINER):
        raise ValueError("Invalid test container name")
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "psql",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=statement,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


@pytest.fixture
def database():
    sql(SCHEMA)


async def score(monkeypatch):
    async def run_query(_session, _path, _args, _db, query, _work_dir, _password):
        return json.loads(sql(query))

    monkeypatch.setattr(shared, "_resolve_pg_paths", AsyncMock())
    monkeypatch.setattr(shared, "_run_psql_json", run_query)
    session = SimpleNamespace(
        interface=SimpleNamespace(create_dir=AsyncMock()),
        write_file=AsyncMock(),
        file_exists=AsyncMock(return_value=False),
    )
    config = SimpleNamespace(metadata=shared.cfg_for_variant(shared.VARIANTS[0]).to_metadata())
    result = await shared.evaluate_variant_task(config, session)
    return result[0], json.loads(session.write_file.call_args.args[1])


@pytest.mark.asyncio
async def test_valid_split_single_delivery_and_return(database, monkeypatch):
    result, report = await score(monkeypatch)
    assert result == 1.0
    assert report["evidence"]["dropship_done_qty"] == 3
    sql("UPDATE stock_move_line SET quantity=3 WHERE id=1; DELETE FROM stock_move_line WHERE id=2;")
    assert (await score(monkeypatch))[1]["checks"]["dropship_done_acc900_3"]
    sql("INSERT INTO stock_move_line VALUES (7,1,1,8,2,1,1,NULL);")
    assert not (await score(monkeypatch))[1]["checks"]["dropship_done_acc900_3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "check"),
    [
        ("DELETE FROM stock_move_line WHERE id=30", "wh_receipt_and_transfer_completed"),
        ("DELETE FROM stock_move_line WHERE id=32", "wh_receipt_and_transfer_completed"),
        (
            "UPDATE stock_move_line SET location_dest_id=3 WHERE id=20",
            "wh_receipt_and_transfer_completed",
        ),
        (
            "UPDATE stock_picking SET state='cancel' WHERE id=30",
            "wh_receipt_and_transfer_completed",
        ),
        ("DELETE FROM stock_move_line WHERE id=40", "subcontract_rm040_resupplied"),
        (
            "UPDATE stock_move SET origin='OTHER' WHERE id=36; UPDATE stock_picking SET origin='OTHER' WHERE id=34",
            "subcontract_rm040_resupplied",
        ),
        ("UPDATE purchase_order SET partner_id=2 WHERE id=4", "subcontract_rm040_resupplied"),
        ("DELETE FROM stock_move_line WHERE id=34", "fp_pick_and_pack_completed"),
        ("DELETE FROM stock_move_line WHERE id=37", "fp_pick_and_pack_completed"),
        ("UPDATE stock_move SET sale_line_id=3 WHERE id=34", "fp_pick_and_pack_completed"),
    ],
)
async def test_key_completed_transfers_are_required(database, monkeypatch, mutation, check):
    sql(mutation)
    assert not (await score(monkeypatch))[1]["checks"][check]


@pytest.mark.asyncio
async def test_subcontract_supply_can_use_an_intermediate_location(database, monkeypatch):
    sql(
        "INSERT INTO stock_location VALUES (14,'internal','14/');"
        "UPDATE stock_move_line SET location_dest_id=14,quantity=3 WHERE id=40;"
        "INSERT INTO stock_move_line VALUES (41,34,36,7,14,13,3,NULL);"
    )
    assert (await score(monkeypatch))[1]["checks"]["subcontract_rm040_resupplied"]


@pytest.mark.asyncio
async def test_subcontract_supply_scores_before_receipt_and_accepts_tagged_manual_move(
    database, monkeypatch
):
    sql(
        "UPDATE stock_move SET state='assigned' WHERE id=3;"
        "UPDATE stock_move SET origin='odoo_compact' WHERE id=36;"
        "UPDATE stock_picking SET origin='odoo_compact' WHERE id=34;"
        "DELETE FROM stock_move_line WHERE id IN (3,4,5);"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["subcontract_rm040_resupplied"]
    assert not checks["subcontract_sc300_received_3_serials"]


@pytest.mark.asyncio
async def test_sale_origin_is_accepted_for_manually_created_handling_moves(database, monkeypatch):
    sql(
        "UPDATE stock_picking SET sale_id=NULL WHERE id IN (32,33);"
        "UPDATE stock_move SET sale_line_id=NULL WHERE id IN (34,35);"
    )
    assert (await score(monkeypatch))[1]["checks"]["fp_pick_and_pack_completed"]


@pytest.mark.asyncio
async def test_move_lines_without_picking_id_are_counted_via_move(database, monkeypatch):
    sql("UPDATE stock_move_line SET picking_id=NULL WHERE id IN (1,2,3,4,5,10,11,12,13);")
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["dropship_done_acc900_3"]
    assert checks["subcontract_sc300_received_3_serials"]
    assert checks["SSK0002_in_MyCo"]
    assert checks["partial_delivery_and_invoicing"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE stock_move SET purchase_line_id=NULL WHERE id=3",
        "UPDATE purchase_order SET partner_id=2 WHERE id=4",
        "UPDATE purchase_order SET partner_ref='' WHERE id=4",
        "UPDATE stock_picking SET picking_type_id=3 WHERE id=3",
        "UPDATE stock_move_line SET location_dest_id=2 WHERE move_id=3",
        "UPDATE stock_move_line SET quantity=0 WHERE move_id=3",
        "UPDATE stock_lot SET product_id=1 WHERE id IN (4,5,6)",
        "UPDATE stock_move SET state='cancel' WHERE id=3",
    ],
)
async def test_subcontract_serials_require_matching_received_purchase(
    database, monkeypatch, mutation
):
    sql(mutation)
    assert not (await score(monkeypatch))[1]["checks"]["subcontract_sc300_received_3_serials"]


@pytest.mark.asyncio
async def test_subcontract_serials_can_be_received_in_separate_pickings(database, monkeypatch):
    sql(
        "INSERT INTO stock_picking (id,picking_type_id,state,sale_id) VALUES (8,2,'done',NULL);"
        "INSERT INTO stock_move (id,purchase_line_id,picking_id) VALUES (10,5,8);"
        "UPDATE stock_move_line SET move_id=10,picking_id=NULL WHERE id=5;"
    )
    assert (await score(monkeypatch))[1]["checks"]["subcontract_sc300_received_3_serials"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE hr_expense SET state='approved' WHERE id=1",
        "UPDATE hr_expense SET approval_state='draft' WHERE id=1",
        "UPDATE account_move SET payment_state='not_paid' WHERE id=11",
        "UPDATE account_move SET state='draft' WHERE id=11",
        "UPDATE hr_expense SET currency_id=1 WHERE id=1",
        "UPDATE account_analytic_line SET move_line_id=NULL WHERE id=1",
        "DROP TABLE hr_expense",
    ],
)
async def test_expense_requires_paid_approved_linked_expense(database, monkeypatch, mutation):
    sql(mutation)
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["analytic_expense_travel_12345"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "check"),
    [
        ("UPDATE sale_order SET partner_id=2 WHERE id=1", "so_confirmed_exists"),
        ("UPDATE sale_order SET currency_id=2 WHERE id=1", "so_confirmed_exists"),
        ("UPDATE sale_order_line SET price_unit=199 WHERE id=1", "so_lines_fp3_acc3"),
        ("UPDATE sale_order_line SET price_unit=31 WHERE id=2", "so_lines_fp3_acc3"),
    ],
)
async def test_sale_has_required_customer_currency_and_prices(
    database, monkeypatch, mutation, check
):
    sql(mutation)
    assert not (await score(monkeypatch))[1]["checks"][check]


@pytest.mark.asyncio
async def test_sale_to_customer_contact_is_accepted(database, monkeypatch):
    sql(
        "INSERT INTO res_partner (id,ref,commercial_partner_id) VALUES (7,NULL,6); UPDATE sale_order SET partner_id=7 WHERE id=1;"
    )
    assert (await score(monkeypatch))[1]["checks"]["so_confirmed_exists"]


@pytest.mark.asyncio
async def test_partner_codes_in_names_are_accepted_without_prefix_collisions(database, monkeypatch):
    sql("UPDATE res_partner SET name=ref || ' Company', ref=NULL;")
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["so_confirmed_exists"] and checks["landed_cost_rm010_60"]
    assert checks["subcontract_sc300_received_3_serials"]
    assert checks["paid_vendor_bills_cover_purchases_and_freight"]
    sql(
        "UPDATE res_partner SET name='CUST-EU010 Company' WHERE id=6;"
        "UPDATE res_partner SET name='V-CN010 Company' WHERE id=1;"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["so_confirmed_exists"] and not checks["landed_cost_rm010_60"]
    assert not checks["paid_vendor_bills_cover_purchases_and_freight"]


@pytest.mark.asyncio
async def test_unrelated_and_wrong_direction_deliveries_do_not_count(database, monkeypatch):
    sql(
        "UPDATE stock_picking SET sale_id=2 WHERE id IN (1,2); UPDATE stock_move SET sale_line_id=3, purchase_line_id=NULL WHERE id IN (1,2);"
    )
    assert not (await score(monkeypatch))[1]["checks"]["dropship_done_acc900_3"]
    sql(
        "UPDATE stock_picking SET sale_id=1 WHERE id IN (1,2); UPDATE stock_move SET sale_line_id=2 WHERE id IN (1,2); UPDATE stock_move_line SET location_id=3 WHERE id IN (1,2);"
    )
    assert not (await score(monkeypatch))[1]["checks"]["dropship_done_acc900_3"]


@pytest.mark.asyncio
async def test_other_sale_lines_inside_a_linked_picking_do_not_count(database, monkeypatch):
    sql("UPDATE stock_move SET sale_line_id=3, purchase_line_id=NULL WHERE id IN (1,2);")
    assert not (await score(monkeypatch))[1]["checks"]["dropship_done_acc900_3"]


@pytest.mark.asyncio
async def test_absent_warehouse_product_or_lot_does_not_count_as_zero(database, monkeypatch):
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["WH_RM010_zero"] and checks["WH_RM020_zero"] and checks["SSK0002_not_in_WH"]
    sql("DELETE FROM stock_warehouse WHERE code='WH';")
    checks = (await score(monkeypatch))[1]["checks"]
    assert (
        not checks["WH_RM010_zero"]
        and not checks["WH_RM020_zero"]
        and not checks["SSK0002_not_in_WH"]
    )
    sql(
        "INSERT INTO stock_warehouse VALUES (1,'WH',3,'two_steps','ship_only'); DELETE FROM product_template WHERE id=4; DELETE FROM stock_lot WHERE id=2;"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["WH_RM010_zero"] and not checks["SSK0002_not_in_WH"]


@pytest.mark.asyncio
async def test_empty_warehouse_without_receipt_or_return_does_not_score(database, monkeypatch):
    sql("DELETE FROM stock_move_line WHERE id IN (20,21,13);")
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["WH_RM010_zero"] and not checks["WH_RM020_zero"]
    assert not checks["SSK0002_not_in_WH"]


@pytest.mark.asyncio
async def test_workorders_invoices_and_paid_purchase_coverage_are_related(database, monkeypatch):
    sql(
        "UPDATE mrp_workorder SET production_id=999 WHERE id=2; DELETE FROM sale_order_line_invoice_rel WHERE invoice_line_id=8; UPDATE account_move SET payment_state='not_paid' WHERE id=4;"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["workorders_done_at_least2"]
    assert not checks["paid_invoice_460"]
    assert not checks["paid_vendor_bills_cover_purchases_and_freight"]


@pytest.mark.asyncio
async def test_consolidated_paid_vendor_bill_is_accepted(database, monkeypatch):
    sql("UPDATE account_move_line SET move_id=3 WHERE id=7;")
    assert (await score(monkeypatch))[1]["checks"]["paid_vendor_bills_cover_purchases_and_freight"]


@pytest.mark.asyncio
async def test_replacement_purchase_can_use_another_vendor_and_price(database, monkeypatch):
    sql(
        "UPDATE purchase_order SET partner_id=3, currency_id=1 WHERE id=6;"
        "UPDATE purchase_order_line SET price_unit=27 WHERE id=7;"
        "UPDATE account_move SET currency_id=1 WHERE id=7;"
        "UPDATE account_move_line SET price_unit=27 WHERE id=7;"
    )
    assert (await score(monkeypatch))[1]["checks"]["paid_vendor_bills_cover_purchases_and_freight"]
    sql("UPDATE account_move SET payment_state='not_paid' WHERE id=7;")
    assert not (await score(monkeypatch))[1]["checks"][
        "paid_vendor_bills_cover_purchases_and_freight"
    ]


@pytest.mark.asyncio
async def test_freight_bill_must_cover_the_freight_line(database, monkeypatch):
    sql("UPDATE account_move_line SET price_subtotal=10 WHERE id=12;")
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["landed_cost_vendor_bill_paid"]
    assert not checks["paid_vendor_bills_cover_purchases_and_freight"]


@pytest.mark.asyncio
async def test_landed_cost_must_apply_to_the_correct_receipt(database, monkeypatch):
    sql("UPDATE stock_landed_cost_lines SET split_method='equal';")
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["landed_cost_rm010_60"] and not checks["landed_cost_rm020_40"]


@pytest.mark.asyncio
async def test_return_and_credit_require_correct_documents(database, monkeypatch):
    sql(
        "UPDATE account_move SET invoice_origin='OTHER' WHERE id=10; DELETE FROM stock_move_line WHERE id=13;"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["paid_creditnote_200"] and not checks["SSK0002_in_MyCo"]


@pytest.mark.asyncio
async def test_return_via_intermediate_internal_location_is_accepted(database, monkeypatch):
    sql(
        "INSERT INTO stock_location VALUES (5,'internal','5/');"
        "UPDATE stock_move_line SET location_dest_id=5 WHERE id=13;"
    )
    assert (await score(monkeypatch))[1]["checks"]["SSK0002_in_MyCo"]
    sql("UPDATE stock_quant SET quantity=0 WHERE lot_id=2;")
    assert not (await score(monkeypatch))[1]["checks"]["SSK0002_in_MyCo"]


@pytest.mark.asyncio
async def test_wrong_bom_and_operation_are_rejected(database, monkeypatch):
    sql(
        "UPDATE mrp_bom_line SET product_qty=1 WHERE bom_id=1 AND product_id=4; UPDATE mrp_workcenter SET costs_hour=50 WHERE id=2;"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["fp_bom"] and not checks["assembly_operation"]
    assert checks["sa_bom"] and not checks["smt_operation"]


@pytest.mark.asyncio
async def test_configured_boms_score_without_completed_manufacturing(database, monkeypatch):
    sql("DELETE FROM mrp_production;")
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["fp_bom"] and checks["sa_bom"]
    assert checks["assembly_operation"] and checks["smt_operation"]
    assert not checks["mo_fp1000_done_qty3"] and not checks["mo_sa200_done_qty3"]
    sql("INSERT INTO mrp_production VALUES (1,'MO-FP',1,'done',3,'S00027',999);")
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["fp_bom"] and not checks["assembly_operation"]


@pytest.mark.asyncio
async def test_delivery_accepts_unreliable_transaction_timestamps(database, monkeypatch):
    sql(
        "UPDATE account_move SET create_date='2026-01-01 09:59' WHERE id=8;"
        "UPDATE account_move SET create_date='2026-01-01 10:09' WHERE id=9;"
    )
    assert (await score(monkeypatch))[1]["checks"]["partial_delivery_and_invoicing"]
    sql("UPDATE account_move SET create_date='2026-01-01 10:16' WHERE id=8;")
    assert (await score(monkeypatch))[1]["checks"]["partial_delivery_and_invoicing"]
    sql("UPDATE account_move_line SET quantity=1 WHERE id=8;")
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["paid_invoice_460"] and not checks["partial_delivery_and_invoicing"]


@pytest.mark.asyncio
async def test_second_delivery_must_be_the_first_delivery_backorder(database, monkeypatch):
    sql("UPDATE stock_picking SET backorder_id=NULL WHERE id=6;")
    assert not (await score(monkeypatch))[1]["checks"]["partial_delivery_and_invoicing"]


@pytest.mark.asyncio
async def test_shipments_from_another_warehouse_do_not_count(database, monkeypatch):
    sql("UPDATE stock_picking_type SET warehouse_id=1 WHERE id=3;")
    assert not (await score(monkeypatch))[1]["checks"]["partial_delivery_and_invoicing"]


@pytest.mark.asyncio
async def test_rates_and_company_dependent_valuation(database, monkeypatch):
    sql(
        "UPDATE product_category SET property_cost_method=NULL, property_valuation=NULL; INSERT INTO ir_default VALUES (1,1,NULL,1,NULL,'\"fifo\"'),(2,2,NULL,1,NULL,'\"real_time\"');"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert checks["currency_configuration"] and checks["fifo_automated_valuation"]
    sql(
        'UPDATE res_currency_rate SET rate=7 WHERE currency_id=3; UPDATE product_category SET property_cost_method=\'{"1":"standard"}\';'
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert not checks["currency_configuration"] and not checks["fifo_automated_valuation"]


@pytest.mark.asyncio
async def test_multiple_valid_manufacturing_orders_are_accepted(database, monkeypatch):
    sql(
        "UPDATE mrp_production SET product_qty=2 WHERE id=1; INSERT INTO mrp_production VALUES (3,'MO-FP-2',1,'done',1,'S00027',1); INSERT INTO mrp_workorder VALUES (4,3,'done');"
    )
    checks = (await score(monkeypatch))[1]["checks"]
    assert (
        checks["mo_fp1000_done_qty3"] and checks["workorders_done_at_least2"] and checks["fp_bom"]
    )
    sql("UPDATE mrp_workorder SET state='ready' WHERE id=4;")
    assert not (await score(monkeypatch))[1]["checks"]["workorders_done_at_least2"]
