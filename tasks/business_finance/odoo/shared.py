from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PureWindowsPath

import cua_bench as cb
from PIL import Image

from tasks.common_config import GeneralTaskConfig

logger = logging.getLogger(__name__)

LOCAL_ROOT = Path(__file__).resolve().parent
VARIANTS_ROOT = LOCAL_ROOT / "variants"
FIXTURE_OUTPUT_DIRS = {"output_test_pos", "output_test_neg"}


def win_join(*parts: str) -> str:
    return str(PureWindowsPath(*parts))


EVAL_TMP_ROOT = win_join(r"C:\Users\User\AppData\Local\Temp\agenthle_eval", "odoo")


def remote_basename(path: str) -> str:
    return PureWindowsPath(path).name


def is_fixture_output_dir(path: str) -> bool:
    return remote_basename(path) in FIXTURE_OUTPUT_DIRS


def eval_work_dir(task_tag: str) -> str:
    return win_join(EVAL_TMP_ROOT, task_tag)


@dataclass(frozen=True)
class VariantSpec:
    variant_name: str
    run_tag: str
    db_name: str
    variant_note: str


VARIANTS = [
    VariantSpec(
        variant_name="odoo_compact",
        run_tag="odoo_compact",
        db_name="odoo_compact",
        variant_note="Recovered compact legacy variant. The shared Odoo workflow is preserved, with this variant using its own DB and searchable run tag.",
    ),
    VariantSpec(
        variant_name="odoo_shifted",
        run_tag="odoo_shifted",
        db_name="odoo_shifted",
        variant_note="Recovered shifted legacy variant. The operational contract matches the recovered shared Odoo workflow while isolating all work in the shifted DB/tag namespace.",
    ),
    VariantSpec(
        variant_name="odoo_stress",
        run_tag="odoo_stress",
        db_name="odoo_stress",
        variant_note="Recovered stress legacy variant. It reuses the recovered end-to-end workflow but keeps its own DB/tag namespace for replay and grading isolation.",
    ),
    VariantSpec(
        variant_name="odoo_hard",
        run_tag="odoo_hard",
        db_name="odoo_hard",
        variant_note="Recovered hard legacy variant. This is the original end-to-end Odoo scenario that the recovered judger and SQL evidence checks were written for.",
    ),
]

VARIANTS_BY_NAME = {spec.variant_name: spec for spec in VARIANTS}


@dataclass
class OdooTaskConfig(GeneralTaskConfig):
    DOMAIN_NAME: str = "business_finance"
    TASK_NAME: str = "odoo"
    VARIANT_NAME: str = ""
    RUN_TAG: str = ""
    DEFAULT_DB_NAME: str = ""
    VARIANT_NOTE: str = ""

    DEFAULT_ODOO_URL: str = "http://localhost:8069/web"
    DEFAULT_ODOO_SERVICE_NAME: str = "odoo-server-19.0"
    TEMPLATE_DB_NAME: str = "AgentService"
    DEFAULT_PSQL_PATH: str = r"C:\Program Files\PostgreSQL\17\bin\psql.exe"
    DEFAULT_DROPDB_PATH: str = r"C:\Program Files\PostgreSQL\17\bin\dropdb.exe"
    DEFAULT_CREATEDB_PATH: str = r"C:\Program Files\PostgreSQL\17\bin\createdb.exe"
    DEFAULT_PG_DUMP_PATH: str = r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe"
    DEFAULT_PG_RESTORE_PATH: str = r"C:\Program Files\PostgreSQL\17\bin\pg_restore.exe"
    DEFAULT_PSQL_ARGS: str = "-h 127.0.0.1 -p 5432 -U openpg"
    DEFAULT_PG_PASSWORD: str = "openpgpwd"
    DEFAULT_ODOO_PYTHON_PATH: str = r"C:\Program Files\Odoo 19.0.20260416\python\python.exe"
    DEFAULT_ODOO_ADMIN_LOGIN: str = "admin@example.com"
    DEFAULT_ODOO_ADMIN_PASSWORD: str = "admin"

    @property
    def variant_source_dir(self) -> Path:
        return VARIANTS_ROOT / self.VARIANT_NAME

    @property
    def prompt_path(self) -> str:
        return win_join(self.task_dir, "input", "prompt.txt")

    @property
    def launch_script_path(self) -> str:
        return win_join(self.software_dir, "launch_odoo.cmd")

    @property
    def task_description(self) -> str:
        # Only the published variant (odoo_compact) ships its prompt.txt with
        # the repo. Unpublished variants have no vendored prompt file, so fall
        # back to the variant note instead of letting a missing file crash the
        # whole module load (load() eagerly builds every variant's description).
        prompt_file = self.variant_source_dir / "input" / "prompt.txt"
        try:
            prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            prompt_text = self.VARIANT_NOTE
        out_dir = self.remote_output_dir
        return f"""\
Goal:
Complete the recovered end-to-end Odoo supply-chain workflow and leave the final business state inside the task database.

Variant:
- {self.VARIANT_NAME}
- {self.VARIANT_NOTE}

Recovered prompt:
{prompt_text}

Inputs / Environment:
- The benchmark data for this variant is staged under {self.task_dir}.
- The local Odoo web server is at {self.DEFAULT_ODOO_URL}. Prepare or restore the environment as needed to complete the task.
- If the Odoo service is available, use {self.launch_script_path} to open the correct database in a browser.
- Task setup attempts to reset database "{self.DEFAULT_DB_NAME}" from template "{self.TEMPLATE_DB_NAME}".
- You must work ONLY inside database "{self.DEFAULT_DB_NAME}".
- Use the run tag "{self.RUN_TAG}" in searchable fields such as Reference / Notes / Customer Ref / Vendor Ref on key documents you create manually.
- The agent-visible task prompt is staged at {self.prompt_path}.
- Odoo web login: {self.DEFAULT_ODOO_ADMIN_LOGIN} / {self.DEFAULT_ODOO_ADMIN_PASSWORD} (task setup attempts to set these credentials).

Warehouses:
- Inbound warehouse code: "WH" (location tree rooted at "WH/Stock")
- Production & Shipping warehouse code: "My Co" (rooted at "My Co/Stock")

Output:
Save these files into {out_dir}
1. lc_split.png
2. invoices.png
3. return_credit.png
4. stock_wh.png
5. submission.txt
"""

    def to_metadata(self) -> dict:
        md = super().to_metadata()
        md.update(
            {
                "run_tag": self.RUN_TAG,
                "odoo_url": self.DEFAULT_ODOO_URL,
                "template_db": self.TEMPLATE_DB_NAME,
                "db_name": self.DEFAULT_DB_NAME,
                "psql_path": self.DEFAULT_PSQL_PATH,
                "dropdb_path": self.DEFAULT_DROPDB_PATH,
                "createdb_path": self.DEFAULT_CREATEDB_PATH,
                "pg_dump_path": self.DEFAULT_PG_DUMP_PATH,
                "pg_restore_path": self.DEFAULT_PG_RESTORE_PATH,
                "psql_args": self.DEFAULT_PSQL_ARGS,
                "pg_password": self.DEFAULT_PG_PASSWORD,
                "odoo_service_name": self.DEFAULT_ODOO_SERVICE_NAME,
                "odoo_python_path": self.DEFAULT_ODOO_PYTHON_PATH,
                "odoo_admin_login": self.DEFAULT_ODOO_ADMIN_LOGIN,
                "odoo_admin_password": self.DEFAULT_ODOO_ADMIN_PASSWORD,
                "variant_note": self.VARIANT_NOTE,
                "prompt_path": self.prompt_path,
                "launch_script_path": self.launch_script_path,
            }
        )
        return md


def cfg_for_variant(spec: VariantSpec) -> OdooTaskConfig:
    return OdooTaskConfig(
        VARIANT_NAME=spec.variant_name,
        RUN_TAG=spec.run_tag,
        DEFAULT_DB_NAME=spec.db_name,
        VARIANT_NOTE=spec.variant_note,
    )


def build_task(spec: VariantSpec) -> cb.Task:
    cfg = cfg_for_variant(spec)
    return cb.Task(
        description=cfg.task_description,
        metadata=cfg.to_metadata(),
        computer={"provider": "computer", "setup_config": {"os_type": "windows"}},
    )


async def _run_cmd(session: cb.DesktopSession, cmd: str, timeout: float = 240.0) -> dict:
    return await session.run_command(cmd, check=False)


# Discover the PostgreSQL CLI tools + Odoo's bundled python on the VM instead of trusting
# hardcoded absolute paths. The image's PostgreSQL major version and Odoo installer build
# number drift between dev VMs / re-bakes (e.g. a standalone "PostgreSQL\17" vs Odoo's own
# bundled PostgreSQL 12, and "Odoo 19.0.<datestamp>"), so a literal path silently breaks the
# DB reset + every eval query. Globbing the conventional install roots is build-agnostic.
_RESOLVE_PG_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$roots = @('C:\Program Files\PostgreSQL\*\bin', 'C:\Program Files\Odoo 19.0.*\PostgreSQL\bin')
$o = @{}
foreach ($t in 'psql','dropdb','createdb','pg_dump','pg_restore') {
  $cands = $roots | ForEach-Object { Join-Path $_ ($t + '.exe') }
  $f = Get-ChildItem $cands -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($f) { $o[$t] = $f.FullName }
}
$py = Get-ChildItem 'C:\Program Files\Odoo 19.0.*\python\python.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($py) { $o['odoo_python'] = $py.FullName }
$o | ConvertTo-Json -Compress
"""


async def _resolve_pg_paths(session: cb.DesktopSession, metadata: dict, work_dir: str) -> None:
    """Resolve psql/dropdb/createdb/pg_dump/pg_restore + the Odoo python on the VM and
    overwrite the metadata defaults in place. Anything not discovered keeps its default."""
    try:
        ps_path = win_join(work_dir, "_resolve_pg.ps1")
        await session.write_file(ps_path, _RESOLVE_PG_PS)
        res = await session.run_command(
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{ps_path}"', check=False
        )
        raw = (res.get("stdout") or "").strip()
        found = json.loads(raw.splitlines()[-1]) if raw else {}
    except Exception as exc:  # pragma: no cover - discovery is best-effort
        logger.warning("pg path resolution failed (using metadata defaults): %s", exc)
        return
    mapping = {
        "psql": "psql_path",
        "dropdb": "dropdb_path",
        "createdb": "createdb_path",
        "pg_dump": "pg_dump_path",
        "pg_restore": "pg_restore_path",
        "odoo_python": "odoo_python_path",
    }
    for key, mkey in mapping.items():
        if found.get(key):
            metadata[mkey] = found[key]


class PsqlCommandError(RuntimeError):
    def __init__(self, status: int, stderr: str | None):
        self.status = status
        super().__init__(f"psql failed rc={status}, stderr={stderr}")


async def _run_psql_json(
    session: cb.DesktopSession,
    psql_path: str,
    psql_args: str,
    db_name: str,
    sql_text: str,
    work_dir: str,
    pg_password: str | None,
):
    sql_path = win_join(work_dir, "autograde.sql")
    await session.write_file(sql_path, sql_text)

    password_assignment = f"$env:PGPASSWORD='{pg_password}'; " if pg_password else ""
    marker = "__ALE_PSQL_STATUS__="
    cmd = (
        'powershell -NoProfile -Command "'
        f"{password_assignment}& '{psql_path}' {psql_args} -d {db_name} -v ON_ERROR_STOP=1 -t -A -q -f \\\"{sql_path}\\\"; "
        f"$psql_status=$LASTEXITCODE; Write-Output ('{marker}' + $psql_status); exit $psql_status\""
    )
    res = await session.run_command(cmd, check=False)
    raw = (res.get("stdout") or "").strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith(marker):
        raise RuntimeError(f"psql status not reported, stderr={res.get('stderr')}")
    try:
        status = int(lines.pop()[len(marker):])
    except ValueError as exc:
        raise RuntimeError(f"invalid psql status, stderr={res.get('stderr')}") from exc
    if status != 0:
        raise PsqlCommandError(status, res.get("stderr"))
    if not lines:
        raise RuntimeError("psql returned empty output")
    return json.loads(lines[-1])


async def _reset_db(
    session: cb.DesktopSession,
    *,
    dropdb_path: str,
    createdb_path: str,
    pg_dump_path: str,
    pg_restore_path: str,
    psql_path: str,
    psql_args: str,
    pg_password: str,
    template_db: str,
    target_db: str,
    work_dir: str,
    odoo_service_name: str = "odoo-server-19.0",
):
    # Stop Odoo service so it does not hold connections during DB reset
    await _run_cmd(session, f'powershell -NoProfile -Command "Stop-Service -Name \'{odoo_service_name}\' -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 5"')

    try:
        terminate_sql = (
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname='{target_db}' AND pid<>pg_backend_pid();"
        )
        await _run_psql_json(session, psql_path, psql_args, "postgres", "SELECT json_build_object('ok', true)::text;", work_dir, pg_password)
        await _run_psql_json(
            session,
            psql_path,
            psql_args,
            "postgres",
            f"SELECT json_build_object('terminated', (SELECT COUNT(*) FROM ({terminate_sql}) t))::text;",
            work_dir,
            pg_password,
        )
    except Exception:
        pass

    cmd_drop = (
        'powershell -NoProfile -Command '
        f'"$env:PGPASSWORD=\'{pg_password}\'; '
        f"& '{dropdb_path}' {psql_args} --if-exists {target_db}\""
    )
    drop_res = await _run_cmd(session, cmd_drop, timeout=240.0)
    if drop_res.get("return_code", 0) != 0:
        logger.warning("dropdb %s failed: %s", target_db, drop_res.get("stderr"))

    cmd_create_template = (
        'powershell -NoProfile -Command '
        f'"$env:PGPASSWORD=\'{pg_password}\'; '
        f"& '{createdb_path}' {psql_args} -T {template_db} {target_db}\""
    )
    res = await _run_cmd(session, cmd_create_template, timeout=360.0)
    if res.get("return_code", 0) == 0:
        # Restart Odoo service after successful DB reset
        await _run_cmd(session, f'powershell -NoProfile -Command "Start-Service -Name \'{odoo_service_name}\' -ErrorAction SilentlyContinue"')
        return {"method": "createdb -T"}

    dump_path = win_join(work_dir, "template_dump.dump")
    cmd_dump = (
        'powershell -NoProfile -Command '
        f'"$env:PGPASSWORD=\'{pg_password}\'; '
        f"& '{pg_dump_path}' {psql_args} -Fc -f \\\"{dump_path}\\\" {template_db}\""
    )
    res = await _run_cmd(session, cmd_dump, timeout=900.0)
    if res.get("return_code", 0) != 0:
        raise RuntimeError(f"pg_dump failed: {res.get('stderr')}")

    cmd_create_empty = (
        'powershell -NoProfile -Command '
        f'"$env:PGPASSWORD=\'{pg_password}\'; '
        f"& '{createdb_path}' {psql_args} {target_db}\""
    )
    res = await _run_cmd(session, cmd_create_empty, timeout=240.0)
    if res.get("return_code", 0) != 0:
        raise RuntimeError(f"createdb failed: {res.get('stderr')}")

    cmd_restore = (
        'powershell -NoProfile -Command '
        f'"$env:PGPASSWORD=\'{pg_password}\'; '
        f"& '{pg_restore_path}' {psql_args} -d {target_db} \\\"{dump_path}\\\"\""
    )
    res = await _run_cmd(session, cmd_restore, timeout=1200.0)
    if res.get("return_code", 0) != 0:
        await _run_cmd(session, 'powershell -NoProfile -Command "Start-Service -Name OdooServer -ErrorAction SilentlyContinue"')
        raise RuntimeError(f"pg_restore failed: {res.get('stderr')}")

    # Restart Odoo service after successful DB reset
    await _run_cmd(session, 'powershell -NoProfile -Command "Start-Service -Name OdooServer -ErrorAction SilentlyContinue"')
    return {"method": "pg_dump/pg_restore"}


async def _reset_admin_credentials(
    session: cb.DesktopSession,
    *,
    work_dir: str,
    db_name: str,
    pg_password: str,
    odoo_python_path: str,
    admin_login: str,
    admin_password: str,
) -> None:
    script = (
        "import psycopg2\n"
        "from passlib.hash import pbkdf2_sha512\n"
        f"pw_hash = pbkdf2_sha512.hash({admin_password!r})\n"
        f"conn = psycopg2.connect(host='127.0.0.1', port=5432, user='openpg', password={pg_password!r}, dbname={db_name!r})\n"
        "cur = conn.cursor()\n"
        f"cur.execute('UPDATE res_users SET login = %s, password = %s WHERE id = 2', ({admin_login!r}, pw_hash))\n"
        "conn.commit()\n"
        "cur.close()\n"
        "conn.close()\n"
        "print('OK')\n"
    )
    script_path = win_join(work_dir, "reset_admin.py")
    await session.write_file(script_path, script)
    res = await _run_cmd(session, f'"{odoo_python_path}" "{script_path}"', timeout=120.0)
    stdout = res.get("stdout") or ""
    if "OK" not in stdout:
        raise RuntimeError(f"admin credential reset failed: stdout={stdout}, stderr={res.get('stderr')}")


async def _write_report(session: cb.DesktopSession, *, out_dir: str, work_dir: str, report: dict) -> None:
    payload = json.dumps(report, indent=2)
    await session.write_file(win_join(work_dir, "autograde_report.json"), payload)
    if not is_fixture_output_dir(out_dir):
        await session.write_file(win_join(out_dir, "autograde_report.json"), payload)


async def start_variant_task(task_cfg, session: cb.DesktopSession) -> None:
    out_dir = task_cfg.metadata["remote_output_dir"]
    template_db = task_cfg.metadata["template_db"]
    db_name = task_cfg.metadata["db_name"]
    work_dir = eval_work_dir(task_cfg.metadata["variant_name"])

    await session.interface.create_dir(out_dir)
    await session.interface.create_dir(work_dir)
    await _resolve_pg_paths(session, task_cfg.metadata, work_dir)

    try:
        info = await _reset_db(
            session,
            dropdb_path=task_cfg.metadata["dropdb_path"],
            createdb_path=task_cfg.metadata["createdb_path"],
            pg_dump_path=task_cfg.metadata["pg_dump_path"],
            pg_restore_path=task_cfg.metadata["pg_restore_path"],
            psql_path=task_cfg.metadata["psql_path"],
            psql_args=task_cfg.metadata["psql_args"],
            pg_password=task_cfg.metadata["pg_password"],
            template_db=template_db,
            target_db=db_name,
            work_dir=work_dir,
            odoo_service_name=task_cfg.metadata.get("odoo_service_name", "odoo-server-19.0"),
        )
        await session.write_file(win_join(work_dir, "RESET_OK.txt"), f"Reset OK. method={info.get('method')}\n")
        try:
            await _reset_admin_credentials(
                session,
                work_dir=work_dir,
                db_name=db_name,
                pg_password=task_cfg.metadata["pg_password"],
                odoo_python_path=task_cfg.metadata["odoo_python_path"],
                admin_login=task_cfg.metadata["odoo_admin_login"],
                admin_password=task_cfg.metadata["odoo_admin_password"],
            )
        except Exception as exc_cred:
            logger.warning("admin credential reset failed (non-fatal): %s", exc_cred)
    except Exception as exc:
        await session.write_file(win_join(work_dir, "RESET_FAILED.txt"), str(exc))

    await session.write_file(
        win_join(work_dir, "README_SETUP.txt"),
        "Setup only prepares the runtime output directory and attempts a DB reset. It does not launch Odoo or clear evaluator fixtures.\n",
    )


def _float_eq(a, b, tol=0.01) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False


async def evaluate_variant_task(task_cfg, session: cb.DesktopSession) -> list[float]:
    out_dir = task_cfg.metadata["remote_output_dir"]
    db_name = task_cfg.metadata["db_name"]
    run_tag = task_cfg.metadata["run_tag"]
    work_dir = eval_work_dir(task_cfg.metadata["variant_name"])
    await session.interface.create_dir(work_dir)
    await _resolve_pg_paths(session, task_cfg.metadata, work_dir)

    report = {
        "task_tag": task_cfg.metadata["variant_name"],
        "run_tag": run_tag,
        "db_name": db_name,
        "checks": {},
        "evidence": {},
        "delivery_checks": {},
    }

    preflight_sql = r"""
SELECT json_build_object(
  'product_template', to_regclass('public.product_template'),
  'product_product', to_regclass('public.product_product'),
  'sale_order', to_regclass('public.sale_order'),
  'sale_order_line', to_regclass('public.sale_order_line'),
  'purchase_order', to_regclass('public.purchase_order'),
  'purchase_order_line', to_regclass('public.purchase_order_line'),
  'account_move', to_regclass('public.account_move'),
  'account_move_line', to_regclass('public.account_move_line'),
  'sale_order_line_invoice_rel', to_regclass('public.sale_order_line_invoice_rel'),
  'res_currency', to_regclass('public.res_currency'),
  'res_currency_rate', to_regclass('public.res_currency_rate'),
  'res_company', to_regclass('public.res_company'),
  'res_partner', to_regclass('public.res_partner'),
  'product_category', to_regclass('public.product_category'),
  'ir_model_fields', to_regclass('public.ir_model_fields'),
  'ir_default', to_regclass('public.ir_default'),
  'stock_quant', to_regclass('public.stock_quant'),
  'stock_lot', to_regclass('public.stock_lot'),
  'stock_scrap', to_regclass('public.stock_scrap'),
  'stock_location', to_regclass('public.stock_location'),
  'stock_warehouse', to_regclass('public.stock_warehouse'),
  'stock_picking', to_regclass('public.stock_picking'),
  'stock_picking_type', to_regclass('public.stock_picking_type'),
  'stock_move_line', to_regclass('public.stock_move_line'),
  'stock_move', to_regclass('public.stock_move'),
  'mrp_production', to_regclass('public.mrp_production'),
  'mrp_workorder', to_regclass('public.mrp_workorder'),
  'mrp_bom', to_regclass('public.mrp_bom'),
  'mrp_bom_line', to_regclass('public.mrp_bom_line'),
  'mrp_routing_workcenter', to_regclass('public.mrp_routing_workcenter'),
  'mrp_workcenter', to_regclass('public.mrp_workcenter'),
  'stock_landed_cost', to_regclass('public.stock_landed_cost'),
  'stock_landed_cost_lines', to_regclass('public.stock_landed_cost_lines'),
  'stock_valuation_adjustment_lines', to_regclass('public.stock_valuation_adjustment_lines'),
  'account_analytic_line', to_regclass('public.account_analytic_line'),
  'account_analytic_account', to_regclass('public.account_analytic_account'),
  'hr_expense', to_regclass('public.hr_expense')
)::text;
"""
    try:
        pre = await _run_psql_json(
            session,
            task_cfg.metadata["psql_path"],
            task_cfg.metadata["psql_args"],
            db_name,
            preflight_sql,
            work_dir,
            task_cfg.metadata["pg_password"],
        )
        report["evidence"]["preflight"] = pre
        needed = [
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
        ok = all(pre.get(item) is not None for item in needed)
        report["checks"]["db_preflight_ok"] = ok
        if not ok:
            await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
            return [0.0]
    except PsqlCommandError as exc:
        report["checks"]["db_preflight_ok"] = False
        report["checks"]["db_preflight_error"] = str(exc)
        await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
        if exc.status == 2:
            return [0.0]
        raise RuntimeError(f"Odoo database preflight failed: {exc}") from exc
    except Exception as exc:
        report["checks"]["db_preflight_ok"] = False
        report["checks"]["db_preflight_error"] = str(exc)
        await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
        raise RuntimeError(f"Odoo database preflight failed: {exc}") from exc

    schema_sql = r'''
SELECT json_build_object(
  'module_columns_ok', NOT EXISTS (
    SELECT 1 FROM (VALUES ('stock_move','sale_line_id'), ('stock_move','purchase_line_id'),
                          ('stock_picking','sale_id'), ('purchase_order_line','sale_line_id'),
                          ('account_move_line','purchase_line_id')) AS required(table_name, column_name)
    WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns column_info
                      WHERE column_info.table_schema='public' AND column_info.table_name=required.table_name
                        AND column_info.column_name=required.column_name)
  ),
  'sml_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='qty_done') THEN 'qty_done'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='quantity') THEN 'quantity'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_move_line' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END,
  'pol_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='purchase_order_line' AND column_name='product_qty') THEN 'product_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='purchase_order_line' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END,
  'scrap_qty_col',
    CASE
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='scrap_qty') THEN 'scrap_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='product_qty') THEN 'product_qty'
      WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='stock_scrap' AND column_name='product_uom_qty') THEN 'product_uom_qty'
      ELSE NULL
    END
)::text;
'''
    try:
        schema = await _run_psql_json(
            session,
            task_cfg.metadata["psql_path"],
            task_cfg.metadata["psql_args"],
            db_name,
            schema_sql,
            work_dir,
            task_cfg.metadata["pg_password"],
        )
        report["evidence"]["schema"] = schema
        if schema.get("module_columns_ok") is False:
            report["checks"]["required_odoo_modules_installed"] = False
            await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
            return [0.0]
    except Exception as exc:
        schema = {}
        report["evidence"]["schema_error"] = str(exc)

    sml_qty_col = schema.get("sml_qty_col") or "qty_done"
    if sml_qty_col not in ("qty_done", "quantity", "product_uom_qty"):
        sml_qty_col = "qty_done"
    pol_qty_col = schema.get("pol_qty_col") or "product_qty"
    if pol_qty_col not in ("product_qty", "product_uom_qty"):
        pol_qty_col = "product_qty"
    scrap_qty_col = schema.get("scrap_qty_col") or "scrap_qty"
    if scrap_qty_col not in ("scrap_qty", "product_qty", "product_uom_qty"):
        scrap_qty_col = "scrap_qty"

    expense_sql = "SELECT 0 AS cnt"
    if pre.get("hr_expense"):
        expense_sql = f"""
  SELECT COUNT(*) AS cnt
  FROM account_analytic_line aal
  JOIN account_analytic_account aaa ON aaa.id = aal.account_id
  JOIN account_move_line aml ON aml.id = aal.move_line_id
  JOIN account_move am ON am.id = aml.move_id
  JOIN hr_expense expense ON expense.account_move_id = am.id
  JOIN res_currency currency ON currency.id = expense.currency_id AND currency.name='USD'
  WHERE aaa.name::text ILIKE '%AN-{run_tag}%'
    AND aal.name::text ILIKE '%Travel to Berlin%'
    AND ABS(aal.amount + 123.45) < 0.01
    AND expense.name='Travel to Berlin' AND ABS(expense.total_amount_currency - 123.45) < 0.01
    AND expense.state='paid' AND expense.approval_state='approved'
    AND am.state='posted' AND am.payment_state='paid'
"""

    sql = f"""
WITH
prod AS (
  SELECT pp.id AS product_id, pt.default_code
  FROM product_product pp
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  WHERE pt.default_code IN ('FP-1000','SA-200','SC-300','RM-010','RM-020','RM-030','RM-040','ACC-900','FREIGHT-100')
),
eur AS (SELECT id AS currency_id FROM res_currency WHERE name='EUR' LIMIT 1),
lc AS (
  SELECT slc.id
  FROM stock_landed_cost slc
  JOIN stock_valuation_adjustment_lines sval ON sval.cost_id = slc.id
  JOIN prod p ON p.product_id = sval.product_id
  JOIN stock_landed_cost_lines cost_line ON cost_line.id = sval.cost_line_id
  JOIN prod freight ON freight.product_id = cost_line.product_id AND freight.default_code='FREIGHT-100'
  JOIN stock_move move ON move.id = sval.move_id
  JOIN purchase_order_line pol ON pol.id = move.purchase_line_id
  JOIN purchase_order po ON po.id = pol.order_id
  JOIN res_partner vendor ON vendor.id = po.partner_id
    AND (vendor.ref='V-CN01' OR vendor.name='V-CN01' OR vendor.name LIKE 'V-CN01 %')
  WHERE slc.state = 'done' AND p.default_code IN ('RM-010','RM-020')
    AND cost_line.split_method='by_current_cost_price' AND ABS(cost_line.price_unit - 100) < 0.01
    AND (COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
         OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
         OR COALESCE(po.name,'') ILIKE '%{run_tag}%')
    AND (COALESCE(slc.name,'') ILIKE '%{run_tag}%' OR COALESCE(slc.description,'') ILIKE '%{run_tag}%')
  GROUP BY slc.id, po.id
  HAVING COUNT(DISTINCT p.default_code) = 2 AND ABS(SUM(sval.additional_landed_cost) - 100.0) < 0.0001
  ORDER BY slc.id DESC
  LIMIT 1
),
lc_split AS (
  SELECT p.default_code, ROUND(SUM(sval.additional_landed_cost)::numeric, 2) AS add_cost
  FROM stock_valuation_adjustment_lines sval
  JOIN lc ON lc.id = sval.cost_id
  JOIN prod p ON p.product_id = sval.product_id
  GROUP BY p.default_code
),
lc_bill_paid AS (
  SELECT COUNT(DISTINCT am.id) AS cnt
  FROM stock_landed_cost slc
  JOIN lc ON lc.id = slc.id
  JOIN account_move am ON (am.id = slc.vendor_bill_id OR COALESCE(am.ref,'') ILIKE '%{run_tag}%'
                           OR COALESCE(am.invoice_origin,'') ILIKE '%{run_tag}%'
                           OR COALESCE(am.narration,'') ILIKE '%{run_tag}%')
  JOIN res_currency currency ON currency.id = am.currency_id AND currency.name = 'USD'
  JOIN account_move_line aml ON aml.move_id = am.id
  JOIN prod product ON product.product_id = aml.product_id AND product.default_code = 'FREIGHT-100'
  WHERE am.move_type='in_invoice' AND am.state='posted' AND am.payment_state='paid'
    AND ABS(aml.price_subtotal - 100) < 0.01
),
so_candidate AS (
  SELECT so.id, so.name, so.company_id,
         SUM(CASE WHEN pt.default_code='FP-1000' THEN sol.product_uom_qty ELSE 0 END) AS fp_qty,
         SUM(CASE WHEN pt.default_code='ACC-900' THEN sol.product_uom_qty ELSE 0 END) AS acc_qty,
         BOOL_OR(customer.ref='CUST-EU01' OR commercial.ref='CUST-EU01'
                 OR customer.name='CUST-EU01' OR customer.name LIKE 'CUST-EU01 %'
                 OR commercial.name='CUST-EU01' OR commercial.name LIKE 'CUST-EU01 %') AS customer_ok,
         MAX(currency.name) AS currency_name,
         BOOL_AND(CASE WHEN sol.product_uom_qty = 0 THEN TRUE
                       WHEN pt.default_code='FP-1000' THEN COALESCE(ABS(sol.price_unit - 200) < 0.01, FALSE)
                       WHEN pt.default_code='ACC-900' THEN COALESCE(ABS(sol.price_unit - 30) < 0.01, FALSE)
                       ELSE FALSE END) AS prices_ok,
         BOOL_OR(
           COALESCE(so.client_order_ref,'') ILIKE '%{run_tag}%'
           OR COALESCE(so.origin,'') ILIKE '%{run_tag}%'
           OR COALESCE(so.note,'') ILIKE '%{run_tag}%'
           OR COALESCE(so.name,'') ILIKE '%{run_tag}%'
         ) AS has_tag,
         MAX(so.state) AS state
  FROM sale_order so
  JOIN sale_order_line sol ON sol.order_id = so.id
  JOIN product_product pp ON pp.id = sol.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  LEFT JOIN res_partner customer ON customer.id = so.partner_id
  LEFT JOIN res_partner commercial ON commercial.id = customer.commercial_partner_id
  LEFT JOIN res_currency currency ON currency.id = so.currency_id
  WHERE pt.default_code IN ('FP-1000','ACC-900')
  GROUP BY so.id, so.name, so.company_id
),
so AS (
  SELECT * FROM so_candidate WHERE has_tag ORDER BY id DESC LIMIT 1
),
company AS (
  SELECT company.* FROM res_company company JOIN so ON so.company_id = company.id
),
currency_rates AS (
  SELECT currency.name, currency.active,
    COALESCE(
      (SELECT rate.rate FROM res_currency_rate rate
       WHERE rate.currency_id = currency.id AND rate.name <= CURRENT_DATE
         AND (rate.company_id IS NULL OR rate.company_id = company.id)
       ORDER BY rate.company_id NULLS LAST, rate.name DESC LIMIT 1),
      (SELECT rate.rate FROM res_currency_rate rate
       WHERE rate.currency_id = currency.id AND (rate.company_id IS NULL OR rate.company_id = company.id)
       ORDER BY rate.company_id NULLS LAST, rate.name ASC LIMIT 1), 1.0
    ) AS rate
  FROM res_currency currency CROSS JOIN company
  WHERE currency.name IN ('USD','EUR','CNY')
),
fx_evidence AS (
  SELECT EXISTS (SELECT 1 FROM company JOIN res_currency currency ON currency.id = company.currency_id WHERE currency.name='USD')
    AND EXISTS (SELECT 1 FROM currency_rates usd, currency_rates eur, currency_rates cny
                WHERE usd.name='USD' AND eur.name='EUR' AND cny.name='CNY'
                  AND eur.active AND cny.active AND eur.rate > 0 AND cny.rate > 0
                  AND ABS(usd.rate / eur.rate - 1.20) < 0.0001
                  AND ABS(usd.rate / cny.rate - 0.125) < 0.0001) AS ok
),
category_defaults AS (
  SELECT field.name,
    (SELECT NULLIF(defaults.json_value::jsonb #>> '{{}}', 'false')
     FROM ir_default defaults
     WHERE defaults.field_id = field.id AND defaults.condition IS NULL
       AND (defaults.user_id IS NULL OR defaults.user_id=1)
       AND (defaults.company_id IS NULL OR defaults.company_id=company.id)
     ORDER BY defaults.user_id NULLS LAST, defaults.company_id NULLS LAST, defaults.id LIMIT 1) AS value
  FROM ir_model_fields field CROSS JOIN company
  WHERE field.model='product.category' AND field.name IN ('property_cost_method','property_valuation')
),
valuation_evidence AS (
  SELECT COUNT(DISTINCT product.default_code) = 8
     AND BOOL_AND(product.is_storable
       AND COALESCE(category.property_cost_method ->> company.id::text,
                    (SELECT value FROM category_defaults WHERE name='property_cost_method'), company.cost_method) = 'fifo'
       AND COALESCE(category.property_valuation ->> company.id::text,
                    (SELECT value FROM category_defaults WHERE name='property_valuation'), company.inventory_valuation) = 'real_time') AS ok
  FROM product_template product
  JOIN product_category category ON category.id = product.categ_id
  CROSS JOIN company
  WHERE product.default_code IN ('FP-1000','SA-200','SC-300','RM-010','RM-020','RM-030','RM-040','ACC-900')
),
dropship_done AS (
  SELECT COALESCE(SUM(CASE WHEN src.usage='supplier' AND dest.usage='customer'
                           THEN sml.{sml_qty_col}
                           WHEN src.usage='customer' AND dest.usage='supplier'
                           THEN -sml.{sml_qty_col} ELSE 0 END), 0) AS qty
  FROM stock_move_line sml
  JOIN stock_move sm ON sm.id = sml.move_id
  JOIN stock_picking sp ON sp.id = sm.picking_id
  JOIN stock_location src ON src.id = sml.location_id
  JOIN stock_location dest ON dest.id = sml.location_dest_id
  LEFT JOIN sale_order_line sol ON sol.id = sm.sale_line_id
  LEFT JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id
  LEFT JOIN sale_order_line purchase_sol ON purchase_sol.id = pol.sale_line_id
  JOIN product_product pp ON pp.id = sml.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  JOIN so ON so.id = COALESCE(sol.order_id, purchase_sol.order_id, sp.sale_id)
  WHERE sp.state='done'
    AND pt.default_code='ACC-900'
),
mo_fp_done AS (
  SELECT mp.id, mp.name, mp.bom_id, mp.product_qty
  FROM mrp_production mp
  JOIN product_product pp ON pp.id = mp.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  JOIN so ON (COALESCE(mp.origin,'') ILIKE '%' || so.name || '%'
              OR COALESCE(mp.origin,'') ILIKE '%{run_tag}%')
  WHERE pt.default_code='FP-1000' AND mp.state='done'
),
mo_sa_done AS (
  SELECT mp.id, mp.name, mp.bom_id, mp.product_qty
  FROM mrp_production mp
  JOIN product_product pp ON pp.id = mp.product_id
  JOIN product_template pt ON pt.id = pp.product_tmpl_id
  JOIN so ON (COALESCE(mp.origin,'') ILIKE '%' || so.name || '%'
              OR COALESCE(mp.origin,'') ILIKE '%{run_tag}%'
              OR EXISTS (SELECT 1 FROM mo_fp_done fp WHERE COALESCE(mp.origin,'') ILIKE '%' || fp.name || '%'))
  WHERE pt.default_code='SA-200' AND mp.state='done'
),
workorders_done AS (
  SELECT EXISTS (SELECT 1 FROM mo_sa_done) AND EXISTS (SELECT 1 FROM mo_fp_done)
     AND NOT EXISTS (SELECT 1 FROM (SELECT id FROM mo_sa_done UNION ALL SELECT id FROM mo_fp_done) mo
                     WHERE NOT EXISTS (SELECT 1 FROM mrp_workorder wo WHERE wo.production_id = mo.id AND wo.state='done')) AS ok
),
bom_requirements(product_code, component_code, quantity) AS (
  VALUES ('FP-1000','SA-200',1), ('FP-1000','SC-300',1), ('FP-1000','RM-010',2),
         ('SA-200','RM-020',1), ('SA-200','RM-030',1), ('SC-300','RM-040',1)
),
valid_boms AS (
  SELECT bom.id, product.default_code AS product_code
  FROM mrp_bom bom
  JOIN product_template product ON product.id = bom.product_tmpl_id
  WHERE bom.active AND bom.product_qty > 0
    AND ((product.default_code IN ('FP-1000','SA-200') AND bom.type='normal')
         OR (product.default_code='SC-300' AND bom.type='subcontract'))
    AND NOT EXISTS (
      SELECT 1 FROM bom_requirements req
      WHERE req.product_code = product.default_code
        AND ABS(COALESCE((SELECT SUM(line.product_qty) FROM mrp_bom_line line
                          JOIN prod component ON component.product_id = line.product_id
                          WHERE line.bom_id = bom.id AND component.default_code = req.component_code), 0)
                - req.quantity * bom.product_qty) > 0.0001
    )
    AND NOT EXISTS (
      SELECT 1 FROM mrp_bom_line line
      LEFT JOIN prod component ON component.product_id = line.product_id
      WHERE line.bom_id = bom.id AND NOT EXISTS (
        SELECT 1 FROM bom_requirements req
        WHERE req.product_code = product.default_code AND req.component_code = component.default_code
      )
    )
),
bom_evidence AS (
  SELECT
    EXISTS (SELECT 1 FROM valid_boms bom WHERE bom.product_code='FP-1000')
      AND NOT EXISTS (SELECT 1 FROM mo_fp_done mo WHERE NOT EXISTS
          (SELECT 1 FROM valid_boms bom WHERE bom.id = mo.bom_id AND bom.product_code='FP-1000')) AS fp,
    EXISTS (SELECT 1 FROM valid_boms bom WHERE bom.product_code='SA-200')
      AND NOT EXISTS (SELECT 1 FROM mo_sa_done mo WHERE NOT EXISTS
          (SELECT 1 FROM valid_boms bom WHERE bom.id = mo.bom_id AND bom.product_code='SA-200')) AS sa,
    EXISTS (SELECT 1 FROM valid_boms bom WHERE bom.product_code='SC-300') AS sc,
    EXISTS (SELECT 1 FROM valid_boms bom
            JOIN mrp_routing_workcenter operation ON operation.bom_id = bom.id
            JOIN mrp_workcenter wc ON wc.id = operation.workcenter_id
            WHERE bom.product_code='FP-1000' AND operation.name::text ILIKE '%Assembly%'
              AND (NOT EXISTS (SELECT 1 FROM mo_fp_done) OR EXISTS (SELECT 1 FROM mo_fp_done mo WHERE mo.bom_id = bom.id))
              AND ABS(operation.time_cycle_manual - 15) < 0.0001 AND ABS(wc.costs_hour - 40) < 0.01) AS assembly,
    EXISTS (SELECT 1 FROM valid_boms bom
            JOIN mrp_routing_workcenter operation ON operation.bom_id = bom.id
            JOIN mrp_workcenter wc ON wc.id = operation.workcenter_id
            WHERE bom.product_code='SA-200' AND operation.name::text ILIKE '%SMT%'
              AND (NOT EXISTS (SELECT 1 FROM mo_sa_done) OR EXISTS (SELECT 1 FROM mo_sa_done mo WHERE mo.bom_id = bom.id))
              AND ABS(operation.time_cycle_manual - 30) < 0.0001 AND ABS(wc.costs_hour - 60) < 0.01) AS smt
),
paid_invoices AS (
  SELECT ROUND(am.amount_total::numeric, 2) AS amount_total,
         SUM(CASE WHEN p.default_code='FP-1000' THEN aml.quantity ELSE 0 END) AS fp_qty,
         SUM(CASE WHEN p.default_code='ACC-900' THEN aml.quantity ELSE 0 END) AS acc_qty
  FROM account_move am
  JOIN eur ON eur.currency_id = am.currency_id
  JOIN account_move_line aml ON aml.move_id = am.id
  JOIN prod p ON p.product_id = aml.product_id
  WHERE am.move_type = 'out_invoice'
    AND am.state = 'posted'
    AND am.payment_state = 'paid'
    AND EXISTS (SELECT 1 FROM sale_order_line_invoice_rel rel
                JOIN sale_order_line sol ON sol.id = rel.order_line_id
                JOIN so ON so.id = sol.order_id
                WHERE rel.invoice_line_id = aml.id)
  GROUP BY am.id, am.amount_total
),
fp_shipments AS (
  SELECT sp.id, sp.date_done, sp.backorder_id,
         SUM(CASE WHEN lot.name='SSK-0001' THEN sml.{sml_qty_col} ELSE 0 END) AS ssk1,
         SUM(CASE WHEN lot.name='SSK-0002' THEN sml.{sml_qty_col} ELSE 0 END) AS ssk2,
         SUM(CASE WHEN lot.name='SSK-0003' THEN sml.{sml_qty_col} ELSE 0 END) AS ssk3,
         SUM(sml.{sml_qty_col}) AS qty
  FROM stock_move_line sml
  JOIN stock_move sm ON sm.id = sml.move_id
  JOIN stock_picking sp ON sp.id = sm.picking_id
  JOIN stock_picking_type picking_type ON picking_type.id = sp.picking_type_id
  JOIN stock_warehouse warehouse ON warehouse.id = picking_type.warehouse_id AND warehouse.code='My Co'
  LEFT JOIN sale_order_line sol ON sol.id = sm.sale_line_id
  JOIN so ON so.id = COALESCE(sol.order_id, sp.sale_id)
  JOIN prod p ON p.product_id = sml.product_id
  JOIN stock_lot lot ON lot.id = sml.lot_id AND lot.product_id = p.product_id
  JOIN stock_location src ON src.id = sml.location_id
  JOIN stock_location dest ON dest.id = sml.location_dest_id
  WHERE sp.state='done' AND p.default_code='FP-1000'
    AND src.usage='internal' AND dest.usage='customer'
  GROUP BY sp.id, sp.date_done, sp.backorder_id
),
delivery_evidence AS (
  SELECT EXISTS (
    SELECT 1 FROM fp_shipments first, fp_shipments second
    WHERE second.backorder_id = first.id AND first.date_done <= second.date_done
      AND ABS(first.qty - 2) < 0.0001 AND ABS(first.ssk1 - 1) < 0.0001
      AND ABS(first.ssk2 - 1) < 0.0001 AND ABS(first.ssk3) < 0.0001
      AND ABS(second.qty - 1) < 0.0001 AND ABS(second.ssk3 - 1) < 0.0001
      AND EXISTS (SELECT 1 FROM paid_invoices first_invoice, paid_invoices second_invoice
                  WHERE ABS(first_invoice.amount_total - 460) < 0.01
                    AND ABS(first_invoice.fp_qty - 2) < 0.0001 AND ABS(first_invoice.acc_qty - 2) < 0.0001
                    AND ABS(second_invoice.amount_total - 230) < 0.01
                    AND ABS(second_invoice.fp_qty - 1) < 0.0001 AND ABS(second_invoice.acc_qty - 1) < 0.0001)
  ) AS ok
),
paid_credit AS (
  SELECT ROUND(am.amount_total::numeric, 2) AS amount_total
  FROM account_move am
  JOIN eur ON eur.currency_id = am.currency_id
  JOIN so ON COALESCE(am.invoice_origin,'') ILIKE '%' || so.name || '%'
    OR EXISTS (SELECT 1 FROM account_move_line line
               JOIN sale_order_line_invoice_rel rel ON rel.invoice_line_id = line.id
               JOIN sale_order_line sol ON sol.id = rel.order_line_id
               WHERE line.move_id = am.id AND sol.order_id = so.id)
  WHERE am.move_type = 'out_refund'
    AND am.state = 'posted'
    AND am.payment_state = 'paid'
    AND EXISTS (SELECT 1 FROM account_move_line line
                JOIN prod p ON p.product_id = line.product_id
                WHERE line.move_id = am.id AND p.default_code='FP-1000'
                  AND ABS(line.quantity - 1) < 0.0001 AND ABS(line.price_subtotal - 200) < 0.01)
    AND NOT EXISTS (SELECT 1 FROM account_move_line line
                    JOIN product_product pp ON pp.id = line.product_id
                    JOIN product_template pt ON pt.id = pp.product_tmpl_id
                    WHERE line.move_id = am.id AND pt.default_code IS DISTINCT FROM 'FP-1000'
                      AND line.price_subtotal <> 0)
),
purchase_requirements(product_code, vendor_ref, currency_name, qty, price) AS (
  VALUES ('RM-010','V-CN01','CNY',10,96), ('RM-020','V-CN01','CNY',10,64),
         ('RM-030','V-US01','USD',3,20), ('RM-040','V-US02','USD',5,7),
         ('SC-300','V-SUB01','USD',3,25), ('ACC-900','V-DS01','USD',3,10),
         ('RM-030',NULL,NULL,1,NULL)
),
paid_purchase_requirements AS (
  SELECT req.product_code, req.qty,
    EXISTS (
      SELECT 1 FROM purchase_order_line pol
      JOIN purchase_order po ON po.id = pol.order_id
      JOIN prod p ON p.product_id = pol.product_id
      JOIN res_partner vendor ON vendor.id = po.partner_id
      JOIN res_currency currency ON currency.id = po.currency_id
      WHERE p.default_code = req.product_code AND (req.vendor_ref IS NULL OR vendor.ref = req.vendor_ref
            OR vendor.name = req.vendor_ref OR vendor.name LIKE req.vendor_ref || ' %')
        AND (req.currency_name IS NULL OR currency.name = req.currency_name) AND po.state IN ('purchase','done')
        AND ABS(pol.{pol_qty_col} - req.qty) < 0.0001
        AND (req.price IS NULL OR ABS(pol.price_unit - req.price) < 0.01)
        AND (COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
             OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
             OR COALESCE(po.name,'') ILIKE '%{run_tag}%')
        AND (SELECT COALESCE(SUM(aml.quantity), 0) FROM account_move_line aml
             JOIN account_move am ON am.id = aml.move_id
             WHERE aml.purchase_line_id = pol.id AND am.move_type = 'in_invoice'
               AND am.state = 'posted' AND am.payment_state = 'paid'
               AND am.currency_id = po.currency_id AND ABS(aml.price_unit - pol.price_unit) < 0.01) >= req.qty - 0.0001
    ) AS paid
  FROM purchase_requirements req
),
fp_lots AS (
  SELECT sl.name
  FROM stock_lot sl
  JOIN prod p ON p.product_id = sl.product_id
  WHERE p.default_code='FP-1000' AND sl.name IN ('SSK-0001','SSK-0002','SSK-0003')
),
sc_lots AS (
  SELECT sl.name
  FROM stock_lot sl
  JOIN prod p ON p.product_id = sl.product_id
  WHERE p.default_code='SC-300' AND sl.name IN ('CS-0001','CS-0002','CS-0003')
),
subcontract_sc_done AS (
  SELECT COUNT(*) AS cnt FROM (
    SELECT sl.name
    FROM stock_move_line sml
    JOIN stock_move sm ON sm.id = sml.move_id
    JOIN stock_picking sp ON sp.id = sm.picking_id
    JOIN stock_picking_type picking_type ON picking_type.id = sp.picking_type_id
    JOIN stock_location destination ON destination.id = sml.location_dest_id
    JOIN stock_lot sl ON sl.id = sml.lot_id AND sl.product_id = sml.product_id
    JOIN prod product ON product.product_id = sml.product_id AND product.default_code='SC-300'
    JOIN purchase_order_line pol ON pol.id = sm.purchase_line_id AND pol.product_id = sml.product_id
    JOIN purchase_order po ON po.id = pol.order_id
    JOIN res_partner vendor ON vendor.id = po.partner_id
      AND (vendor.ref='V-SUB01' OR vendor.name='V-SUB01' OR vendor.name LIKE 'V-SUB01 %')
    WHERE sp.state='done' AND sm.state='done' AND picking_type.code='incoming'
      AND destination.usage='internal' AND po.state IN ('purchase','done')
      AND sl.name IN ('CS-0001','CS-0002','CS-0003')
      AND (COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
           OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
           OR COALESCE(po.name,'') ILIKE '%{run_tag}%')
    GROUP BY sl.name
    HAVING SUM(sml.{sml_qty_col}) >= 0.9999
  ) received
),
wh_base AS (
  SELECT sw.id, sw.code, sw.lot_stock_id, sw.wh_input_stock_loc_id,
         sw.wh_pack_stock_loc_id, sw.wh_output_stock_loc_id, sw.reception_steps, sw.delivery_steps
  FROM stock_warehouse sw
  JOIN stock_location location ON location.id = sw.lot_stock_id AND location.usage = 'internal'
  WHERE sw.code IN ('WH','My Co')
),
wh_locs AS (
  SELECT wb.code AS wh_code, loc.id AS location_id
  FROM wh_base wb
  JOIN stock_location loc
    ON loc.id = wb.lot_stock_id
    OR loc.parent_path LIKE CONCAT('%/', wb.lot_stock_id::text, '/%')
),
wh_received AS (
  SELECT p.default_code AS product_code, SUM(line.{sml_qty_col}) AS qty
  FROM stock_move_line line
  JOIN stock_move move ON move.id = line.move_id AND move.state='done'
  JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
  JOIN stock_picking_type kind ON kind.id = picking.picking_type_id AND kind.code='incoming'
  JOIN stock_warehouse warehouse ON warehouse.id = kind.warehouse_id AND warehouse.code='WH'
  JOIN stock_location source ON source.id = line.location_id AND source.usage='supplier'
  JOIN stock_location destination ON destination.id = line.location_dest_id AND destination.usage='internal'
    AND (destination.id = warehouse.wh_input_stock_loc_id
         OR destination.parent_path LIKE CONCAT('%/', warehouse.wh_input_stock_loc_id, '/%'))
  JOIN purchase_order_line pol ON pol.id = move.purchase_line_id AND pol.product_id = line.product_id
  JOIN purchase_order po ON po.id = pol.order_id AND po.state IN ('purchase','done')
  JOIN res_partner vendor ON vendor.id = po.partner_id
    AND (vendor.ref='V-CN01' OR vendor.name='V-CN01' OR vendor.name LIKE 'V-CN01 %')
  JOIN prod p ON p.product_id = line.product_id AND p.default_code IN ('RM-010','RM-020')
  WHERE COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
     OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
     OR COALESCE(po.name,'') ILIKE '%{run_tag}%'
  GROUP BY p.default_code
),
wh_internal_flow AS (
  SELECT p.default_code,
    SUM(CASE WHEN (source.id = wh.wh_input_stock_loc_id OR source.parent_path LIKE CONCAT('%/', wh.wh_input_stock_loc_id, '/%'))
                   AND (destination.id = wh.lot_stock_id OR destination.parent_path LIKE CONCAT('%/', wh.lot_stock_id, '/%'))
             THEN line.{sml_qty_col} ELSE 0 END) AS putaway_qty,
    SUM(CASE WHEN (source.id = wh.lot_stock_id OR source.parent_path LIKE CONCAT('%/', wh.lot_stock_id, '/%'))
                   AND (destination.id = myco.lot_stock_id OR destination.parent_path LIKE CONCAT('%/', myco.lot_stock_id, '/%'))
             THEN line.{sml_qty_col} ELSE 0 END) AS transfer_qty
  FROM stock_move_line line
  JOIN stock_move move ON move.id = line.move_id AND move.state='done'
  JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
  JOIN stock_location source ON source.id = line.location_id
  JOIN stock_location destination ON destination.id = line.location_dest_id
  JOIN prod p ON p.product_id = line.product_id AND p.default_code IN ('RM-010','RM-020')
  JOIN wh_base wh ON wh.code='WH'
  JOIN wh_base myco ON myco.code='My Co'
  GROUP BY p.default_code
),
wh_workflow AS (
  SELECT COUNT(*) = 2 AND BOOL_AND(received.qty >= 9.9999 AND flow.putaway_qty >= 9.9999
                                    AND flow.transfer_qty >= 9.9999) AS ok
  FROM wh_received received JOIN wh_internal_flow flow ON flow.default_code = received.product_code
),
subcontract_supply AS (
  SELECT EXISTS (
    SELECT 1
    FROM purchase_order po
    JOIN res_partner vendor ON vendor.id = po.partner_id
    JOIN purchase_order_line pol ON pol.order_id = po.id
    JOIN prod contracted ON contracted.product_id = pol.product_id AND contracted.default_code='SC-300'
    JOIN stock_move receipt ON receipt.purchase_line_id = pol.id AND receipt.state <> 'cancel'
    JOIN wh_base myco ON myco.code='My Co'
    WHERE po.state IN ('purchase','done')
      AND (vendor.ref='V-SUB01' OR vendor.name='V-SUB01' OR vendor.name LIKE 'V-SUB01 %')
      AND (COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
           OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
           OR COALESCE(po.name,'') ILIKE '%{run_tag}%')
      AND (SELECT COALESCE(SUM(line.{sml_qty_col}), 0)
           FROM stock_move_line line
           JOIN stock_move move ON move.id = line.move_id AND move.state='done'
           JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
           JOIN stock_location source ON source.id = line.location_id
           JOIN prod material ON material.product_id = line.product_id AND material.default_code='RM-040'
           WHERE (source.id = myco.lot_stock_id OR source.parent_path LIKE CONCAT('%/', myco.lot_stock_id, '/%'))
             AND (move.origin = po.name OR picking.origin = po.name
                  OR COALESCE(move.origin,'') ILIKE '%{run_tag}%'
                  OR COALESCE(picking.origin,'') ILIKE '%{run_tag}%')) >= 2.9999
      AND (SELECT COALESCE(SUM(line.{sml_qty_col}), 0)
           FROM stock_move_line line
           JOIN stock_move move ON move.id = line.move_id AND move.state='done'
           JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
           JOIN prod material ON material.product_id = line.product_id AND material.default_code='RM-040'
           WHERE line.location_dest_id = receipt.location_id
             AND (move.origin = po.name OR picking.origin = po.name
                  OR COALESCE(move.origin,'') ILIKE '%{run_tag}%'
                  OR COALESCE(picking.origin,'') ILIKE '%{run_tag}%')) >= 2.9999
  ) AS ok
),
fp_handling AS (
  SELECT COUNT(*) = 3 AND BOOL_AND(picked >= 0.9999 AND packed >= 0.9999) AS ok
  FROM (
    SELECT lot.name,
      SUM(CASE WHEN (source.id = myco.lot_stock_id OR source.parent_path LIKE CONCAT('%/', myco.lot_stock_id, '/%'))
                     AND (destination.id = myco.wh_pack_stock_loc_id OR destination.parent_path LIKE CONCAT('%/', myco.wh_pack_stock_loc_id, '/%'))
               THEN line.{sml_qty_col} ELSE 0 END) AS picked,
      SUM(CASE WHEN (source.id = myco.wh_pack_stock_loc_id OR source.parent_path LIKE CONCAT('%/', myco.wh_pack_stock_loc_id, '/%'))
                     AND (destination.id = myco.wh_output_stock_loc_id OR destination.parent_path LIKE CONCAT('%/', myco.wh_output_stock_loc_id, '/%'))
               THEN line.{sml_qty_col} ELSE 0 END) AS packed
    FROM stock_move_line line
    JOIN stock_move move ON move.id = line.move_id AND move.state='done'
    JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
    LEFT JOIN sale_order_line sale_line ON sale_line.id = move.sale_line_id
    JOIN so ON so.id = COALESCE(sale_line.order_id, picking.sale_id)
      OR (sale_line.id IS NULL AND picking.sale_id IS NULL
          AND (move.origin = so.name OR picking.origin = so.name))
    JOIN prod product ON product.product_id = line.product_id AND product.default_code='FP-1000'
    JOIN stock_lot lot ON lot.id = line.lot_id AND lot.product_id = line.product_id
      AND lot.name IN ('SSK-0001','SSK-0002','SSK-0003')
    JOIN stock_location source ON source.id = line.location_id
    JOIN stock_location destination ON destination.id = line.location_dest_id
    JOIN wh_base myco ON myco.code='My Co'
    GROUP BY lot.name
  ) handling
),
wh_qty AS (
  SELECT wl.wh_code, p.default_code AS product_code, ROUND(SUM(sq.quantity)::numeric, 6) AS qty
  FROM stock_quant sq
  JOIN wh_locs wl ON wl.location_id = sq.location_id
  JOIN prod p ON p.product_id = sq.product_id
  GROUP BY wl.wh_code, p.default_code
),
ssk2_wh AS (
  SELECT wl.wh_code, ROUND(SUM(sq.quantity)::numeric, 6) AS qty
  FROM stock_quant sq
  JOIN stock_lot sl ON sl.id = sq.lot_id
  JOIN wh_locs wl ON wl.location_id = sq.location_id
  JOIN prod p ON p.product_id = sl.product_id AND p.product_id = sq.product_id
  WHERE sl.name='SSK-0002' AND p.default_code='FP-1000'
  GROUP BY wl.wh_code
),
ssk2_return AS (
  SELECT EXISTS (
    SELECT 1 FROM stock_move_line line
    JOIN stock_move move ON move.id = line.move_id
    JOIN stock_picking picking ON picking.id = move.picking_id AND picking.state='done'
    LEFT JOIN stock_move original ON original.id = move.origin_returned_move_id
    LEFT JOIN sale_order_line original_line ON original_line.id = original.sale_line_id
    JOIN so ON original_line.order_id = so.id OR COALESCE(picking.origin,'') ILIKE '%{run_tag}%'
    JOIN stock_location source ON source.id = line.location_id AND source.usage='customer'
    JOIN stock_location destination ON destination.id = line.location_dest_id AND destination.usage='internal'
    JOIN stock_lot lot ON lot.id = line.lot_id AND lot.name='SSK-0002'
    JOIN prod p ON p.product_id = lot.product_id AND p.product_id = line.product_id AND p.default_code='FP-1000'
    WHERE ABS(line.{sml_qty_col} - 1) < 0.0001
  ) AS ok
),
scrap_rm030 AS (
  SELECT COUNT(*) AS cnt
  FROM stock_scrap ss
  JOIN prod p ON p.product_id = ss.product_id
  WHERE p.default_code='RM-030'
    AND ss.state='done'
    AND ABS(ss.{scrap_qty_col} - 1.0) < 0.0001
),
extra_po_rm030 AS (
  SELECT COUNT(*) AS cnt
  FROM purchase_order po
  JOIN purchase_order_line pol ON pol.order_id = po.id
  JOIN prod p ON p.product_id = pol.product_id
  WHERE p.default_code='RM-030'
    AND ABS(pol.{pol_qty_col} - 1.0) < 0.0001
    AND (
      COALESCE(po.partner_ref,'') ILIKE '%{run_tag}%'
      OR COALESCE(po.origin,'') ILIKE '%{run_tag}%'
      OR COALESCE(po.name,'') ILIKE '%{run_tag}%'
    )
),
analytic_expense AS (
  {expense_sql}
)
SELECT json_build_object(
  'lc_split', (SELECT COALESCE(json_object_agg(default_code, add_cost), '{{}}'::json) FROM lc_split),
  'lc_bill_paid_cnt', (SELECT cnt FROM lc_bill_paid),
  'so', (SELECT COALESCE(json_build_object('id', id, 'name', name, 'fp_qty', fp_qty, 'acc_qty', acc_qty, 'state', state,
          'customer_ok', customer_ok, 'currency_name', currency_name, 'prices_ok', prices_ok), '{{}}'::json) FROM so),
  'currency_configuration', (SELECT ok FROM fx_evidence),
  'inventory_valuation', (SELECT ok FROM valuation_evidence),
  'dropship_done_qty', (SELECT qty FROM dropship_done),
  'mo_sa_done_qty', (SELECT COALESCE(SUM(product_qty), 0) FROM mo_sa_done),
  'mo_fp_done_qty', (SELECT COALESCE(SUM(product_qty), 0) FROM mo_fp_done),
  'workorders_done', (SELECT ok FROM workorders_done),
  'boms', (SELECT row_to_json(bom_evidence) FROM bom_evidence),
  'paid_invoices', (SELECT COALESCE(json_agg(json_build_object('amount', amount_total, 'fp_qty', fp_qty, 'acc_qty', acc_qty) ORDER BY amount_total), '[]'::json) FROM paid_invoices),
  'partial_delivery_and_invoicing', (SELECT ok FROM delivery_evidence),
  'paid_credit', (SELECT COALESCE(json_agg(amount_total ORDER BY amount_total), '[]'::json) FROM paid_credit),
  'paid_purchase_requirements', (SELECT COALESCE(json_agg(json_build_object('product', product_code, 'qty', qty, 'paid', paid)), '[]'::json) FROM paid_purchase_requirements),
  'fp_lots', (SELECT COALESCE(json_agg(name ORDER BY name), '[]'::json) FROM fp_lots),
  'sc_lots', (SELECT COALESCE(json_agg(name ORDER BY name), '[]'::json) FROM sc_lots),
  'subcontract_sc_distinct_lots', (SELECT cnt FROM subcontract_sc_done),
  'subcontract_supply', (SELECT ok FROM subcontract_supply),
  'wh_received', (SELECT COALESCE(json_agg(json_build_object('product', product_code, 'qty', qty)), '[]'::json) FROM wh_received),
  'wh_workflow', (SELECT ok FROM wh_workflow),
  'fp_handling', (SELECT ok FROM fp_handling),
  'wh_qty', (SELECT COALESCE(json_agg(json_build_object('wh', wh_code, 'product', product_code, 'qty', qty) ORDER BY wh_code, product_code), '[]'::json) FROM wh_qty),
  'warehouses', (SELECT COALESCE(json_agg(json_build_object('code', code, 'reception_steps', reception_steps, 'delivery_steps', delivery_steps)), '[]'::json) FROM wh_base),
  'products', (SELECT COALESCE(json_agg(DISTINCT default_code), '[]'::json) FROM prod),
  'ssk2_wh', (SELECT COALESCE(json_agg(json_build_object('wh', wh_code, 'qty', qty) ORDER BY wh_code), '[]'::json) FROM ssk2_wh),
  'ssk2_return', (SELECT ok FROM ssk2_return),
  'scrap_rm030_cnt', (SELECT cnt FROM scrap_rm030),
  'extra_po_rm030_cnt', (SELECT cnt FROM extra_po_rm030),
  'analytic_expense_cnt', (SELECT cnt FROM analytic_expense)
)::text;
"""
    try:
        evidence = await _run_psql_json(
            session,
            task_cfg.metadata["psql_path"],
            task_cfg.metadata["psql_args"],
            db_name,
            sql,
            work_dir,
            task_cfg.metadata["pg_password"],
        )
        report["evidence"].update(evidence)
        report["checks"]["db_evidence_collected"] = True
    except Exception as exc:
        report["checks"]["db_evidence_collected"] = False
        report["checks"]["db_evidence_error"] = str(exc)
        await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
        raise RuntimeError(f"Odoo database evidence query failed: {exc}") from exc

    def wh_map(wh_qty_rows):
        by_wh = {}
        for row in wh_qty_rows:
            if isinstance(row, dict):
                by_wh.setdefault(row.get("wh"), {})
                by_wh[row.get("wh")][row.get("product")] = float(row.get("qty", 0) or 0)
        return by_wh

    lc_split = report["evidence"].get("lc_split") or {}
    inv = report["evidence"].get("paid_invoices") or []
    credit = report["evidence"].get("paid_credit") or []
    fp_lots = set(report["evidence"].get("fp_lots") or [])
    sc_lots = set(report["evidence"].get("sc_lots") or [])
    so = report["evidence"].get("so") or {}
    by_wh = wh_map(report["evidence"].get("wh_qty") or [])
    warehouses = {row.get("code"): row for row in (report["evidence"].get("warehouses") or []) if isinstance(row, dict)}
    products = set(report["evidence"].get("products") or [])
    boms = report["evidence"].get("boms") or {}
    paid_requirements = report["evidence"].get("paid_purchase_requirements") or []
    wh_received = {row.get("product"): float(row.get("qty", 0) or 0)
                   for row in (report["evidence"].get("wh_received") or []) if isinstance(row, dict)}
    ssk2_map = {
        item.get("wh"): float(item.get("qty", 0) or 0)
        for item in (report["evidence"].get("ssk2_wh") or [])
        if isinstance(item, dict)
    }

    def wh_prod_qty(warehouse: str, product: str) -> float:
        return float(by_wh.get(warehouse, {}).get(product, 0) or 0)

    def invoice_matches(amount: float, quantity: float) -> bool:
        return any(isinstance(item, dict) and _float_eq(item.get("amount"), amount)
                   and _float_eq(item.get("fp_qty"), quantity)
                   and _float_eq(item.get("acc_qty"), quantity) for item in inv)

    checks = {
        "landed_cost_rm010_60": _float_eq(lc_split.get("RM-010", 0), 60.00),
        "landed_cost_rm020_40": _float_eq(lc_split.get("RM-020", 0), 40.00),
        "landed_cost_vendor_bill_paid": int(report["evidence"].get("lc_bill_paid_cnt", 0) or 0) >= 1,
        "so_confirmed_exists": bool(so) and str(so.get("state", "")).lower() in ("sale", "done")
        and so.get("customer_ok") is True and so.get("currency_name") == "EUR",
        "so_lines_fp3_acc3": bool(so) and _float_eq(so.get("fp_qty", 0), 3.0)
        and _float_eq(so.get("acc_qty", 0), 3.0) and so.get("prices_ok") is True,
        "dropship_done_acc900_3": _float_eq(report["evidence"].get("dropship_done_qty"), 3.0),
        "mo_sa200_done_qty3": _float_eq(report["evidence"].get("mo_sa_done_qty"), 3.0),
        "mo_fp1000_done_qty3": _float_eq(report["evidence"].get("mo_fp_done_qty"), 3.0),
        "workorders_done_at_least2": report["evidence"].get("workorders_done") is True,
        "paid_invoice_460": invoice_matches(460.00, 2.0),
        "paid_invoice_230": invoice_matches(230.00, 1.0),
        "paid_creditnote_200": any(_float_eq(item, 200.00) for item in credit),
        "paid_vendor_bills_cover_purchases_and_freight": len(paid_requirements) == 7 and all(row.get("paid") is True for row in paid_requirements) and int(report["evidence"].get("lc_bill_paid_cnt", 0) or 0) >= 1,
        "fp_serials_all": fp_lots == {"SSK-0001", "SSK-0002", "SSK-0003"},
        "sc_serials_all": sc_lots == {"CS-0001", "CS-0002", "CS-0003"},
        "subcontract_sc300_received_3_serials": int(report["evidence"].get("subcontract_sc_distinct_lots", 0) or 0) >= 3,
        "subcontract_rm040_resupplied": report["evidence"].get("subcontract_supply") is True,
        "scrap_rm030_qty1_done": int(report["evidence"].get("scrap_rm030_cnt", 0) or 0) >= 1,
        "extra_po_rm030_qty1_exists": int(report["evidence"].get("extra_po_rm030_cnt", 0) or 0) >= 1,
        "analytic_expense_travel_12345": int(report["evidence"].get("analytic_expense_cnt", 0) or 0) >= 1,
        "WH_RM010_zero": "WH" in warehouses and "RM-010" in products and wh_received.get("RM-010", 0) >= 9.9999 and _float_eq(wh_prod_qty("WH", "RM-010"), 0.0),
        "WH_RM020_zero": "WH" in warehouses and "RM-020" in products and wh_received.get("RM-020", 0) >= 9.9999 and _float_eq(wh_prod_qty("WH", "RM-020"), 0.0),
        "MyCo_RM010_4": _float_eq(wh_prod_qty("My Co", "RM-010"), 4.0),
        "MyCo_RM020_7": _float_eq(wh_prod_qty("My Co", "RM-020"), 7.0),
        "SSK0002_in_MyCo": report["evidence"].get("ssk2_return") is True and float(ssk2_map.get("My Co", 0) or 0) >= 0.99,
        "SSK0002_not_in_WH": "WH" in warehouses and "SSK-0002" in fp_lots and report["evidence"].get("ssk2_return") is True
        and float(ssk2_map.get("My Co", 0) or 0) >= 0.99 and _float_eq(float(ssk2_map.get("WH", 0) or 0), 0.0),
        "WH_two_step_receipts": warehouses.get("WH", {}).get("reception_steps") == "two_steps",
        "MyCo_three_step_delivery": warehouses.get("My Co", {}).get("delivery_steps") == "pick_pack_ship",
        "wh_receipt_and_transfer_completed": report["evidence"].get("wh_workflow") is True,
        "fp_pick_and_pack_completed": report["evidence"].get("fp_handling") is True,
        "fp_bom": boms.get("fp") is True,
        "sa_bom": boms.get("sa") is True,
        "subcontract_bom": boms.get("sc") is True,
        "assembly_operation": boms.get("assembly") is True,
        "smt_operation": boms.get("smt") is True,
        "partial_delivery_and_invoicing": report["evidence"].get("partial_delivery_and_invoicing") is True,
        "currency_configuration": report["evidence"].get("currency_configuration") is True,
        "fifo_automated_valuation": report["evidence"].get("inventory_valuation") is True,
    }

    report["score_items"] = [{"name": name, "passed": bool(passed), "weight": 1.0} for name, passed in checks.items()]
    report["checks"].update({name: bool(passed) for name, passed in checks.items()})
    total = len(checks)
    passed = sum(1 for value in checks.values() if value)
    report["final_score"] = round(passed / total, 4) if total > 0 else 0.0
    report["score_detail"] = {"passed": passed, "total": total}

    for name in ("lc_split.png", "invoices.png", "return_credit.png", "stock_wh.png", "submission.txt"):
        path = win_join(out_dir, name)
        if not await session.file_exists(path):
            report["delivery_checks"][name] = {"readable": False, "reason": "missing"}
            continue
        payload = await session.read_bytes(path)
        try:
            if name.endswith(".png"):
                with Image.open(BytesIO(payload)) as image:
                    if image.format != "PNG":
                        raise ValueError("Expected a PNG image")
                    image.verify()
            elif not payload.decode("utf-8-sig").strip():
                raise ValueError("Empty submission note")
        except (OSError, ValueError) as exc:
            report["delivery_checks"][name] = {"readable": False, "reason": str(exc)}
        else:
            report["delivery_checks"][name] = {"readable": True}
    report["scoring_basis"] = "database_checks"

    await _write_report(session, out_dir=out_dir, work_dir=work_dir, report=report)
    return [report["final_score"]]
