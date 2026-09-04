"""Command line: install it, prove it works, then get out of the way.

    commerce-policy migrate --storefront-role app --engine-role policy_engine
    commerce-policy verify
    commerce-policy set-cost SAR-104 --cost 190000 --floor 240000
    commerce-policy check SAR-104 --discount 800

`verify` is the important one. It is what a merchant runs when something looks
wrong, and it answers the four questions that account for nearly every failed
install: can I reach the database, does the schema exist, can this role read
what it needs, and can it read something it should not.
"""

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from .rules import band as compute_band
from .settings import ConfigError, load
from .store import PolicyStore, StoreError
from .version import __version__

MIGRATIONS = Path(__file__).parent / "migrations"


def _connect(settings):
    return psycopg.connect(settings["database_url"], connect_timeout=10)


# ----------------------------------------------------------------------

def cmd_migrate(args, settings):
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        print(f"no migration files in {MIGRATIONS}", file=sys.stderr)
        return 1

    with _connect(settings) as conn:
        with conn.transaction():
            for path in files:
                print(f"applying {path.name}")
                conn.execute(path.read_text(encoding="utf-8"))

            conn.execute(
                "INSERT INTO policy.rules (merchant_id, max_discount_bps, "
                "min_margin_bps) VALUES (%s, %s, %s) "
                "ON CONFLICT (merchant_id) DO NOTHING",
                (settings["merchant_id"], settings["max_discount_bps"],
                 settings["min_margin_bps"]))

            if args.storefront_role:
                # The whole security model in four statements: the role the
                # public website runs as is given no way to reach this schema,
                # so no bug in that website can read a cost.
                role = sql.Identifier(args.storefront_role)
                conn.execute(sql.SQL(
                    "REVOKE ALL ON ALL TABLES IN SCHEMA policy FROM {}"
                ).format(role))
                conn.execute(sql.SQL(
                    "REVOKE ALL ON SCHEMA policy FROM {}").format(role))
                print(f"revoked policy schema from {args.storefront_role}")

            if args.engine_role:
                role = sql.Identifier(args.engine_role)
                conn.execute(sql.SQL(
                    "GRANT USAGE ON SCHEMA policy TO {}").format(role))
                conn.execute(sql.SQL(
                    "GRANT SELECT ON policy.economics, policy.rules TO {}"
                ).format(role))
                conn.execute(sql.SQL(
                    "GRANT SELECT, INSERT ON policy.decisions TO {}"
                ).format(role))
                conn.execute(sql.SQL(
                    "GRANT USAGE ON ALL SEQUENCES IN SCHEMA policy TO {}"
                ).format(role))
                print(f"granted read access to {args.engine_role}")

    print("\nschema ready.")
    if not args.storefront_role:
        print("\nNothing has been revoked from your web application's role "
              "yet.\nUntil you do that, a bug in your website could still "
              "read policy.economics:\n\n"
              "    commerce-policy migrate --storefront-role <your web role>")
    return 0


def cmd_verify(args, settings):
    store = PolicyStore(settings)
    ok = True

    print(f"commerce-policy {__version__}")
    print(f"merchant       {settings['merchant_id']}")
    print(f"control plane  {settings['control_plane_url']}")
    print()

    try:
        with _connect(settings) as conn:
            conn.execute("SELECT 1")
        print("  database        reachable")
    except psycopg.Error as exc:
        print(f"  database        UNREACHABLE — {exc}")
        return 1

    try:
        merchant_rules = store.rules()
        print(f"  policy schema   present")
        print(f"  discount cap    "
              f"{merchant_rules['max_discount_bps'] / 100:.2f}%")
        print(f"  margin floor    "
              f"{merchant_rules['min_margin_bps'] / 100:.2f}%")
    except StoreError as exc:
        print(f"  policy schema   {exc}")
        return 1

    with _connect(settings) as conn:
        counts = conn.execute(
            "SELECT count(*) FILTER (WHERE cost_paise IS NOT NULL) AS costed, "
            "count(*) FILTER (WHERE floor_price_paise IS NOT NULL) AS floored,"
            " count(*) AS total FROM policy.economics").fetchone()
    costed, floored, total = counts
    print(f"  economics       {total} rows "
          f"({costed} with cost, {floored} with a floor)")
    if total == 0:
        print("                  nothing priced yet — margin and floor checks "
              "will report not_configured")

    # A role that can write is a role that can corrupt a live shop, so it is
    # reported as a failure even though everything above passed.
    try:
        with _connect(settings) as conn:
            with conn.transaction():
                conn.execute("CREATE TEMP TABLE _probe (n int)")
                conn.execute("SELECT 1")
                raise _Rollback()
    except _Rollback:
        print("  role            WRITABLE — grant SELECT only")
        ok = False
    except psycopg.Error:
        print("  role            read-only")

    good, broken_at = store.verify_chain()
    print(f"  audit chain     {'intact' if good else f'BROKEN at seq {broken_at}'}")
    ok = ok and good

    print()
    print("ok" if ok else "problems found — see above")
    return 0 if ok else 1


def cmd_set_cost(args, settings):
    if args.cost is None and args.floor is None:
        print("give --cost, --floor, or both", file=sys.stderr)
        return 1

    with _connect(settings) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO policy.economics "
                "(sku, cost_paise, floor_price_paise) VALUES (%s, %s, %s) "
                "ON CONFLICT (sku) DO UPDATE SET "
                "cost_paise = COALESCE(EXCLUDED.cost_paise, "
                "                      policy.economics.cost_paise), "
                "floor_price_paise = COALESCE(EXCLUDED.floor_price_paise, "
                "                      policy.economics.floor_price_paise), "
                "updated_at = now()",
                (args.sku, args.cost, args.floor))
    print(f"{args.sku} updated")
    return 0


def cmd_check(args, settings):
    from .engine import PolicyEngine

    engine = PolicyEngine(settings=settings)
    lines = [{"sku": args.sku, "qty": args.qty}]

    products = engine.store.products([args.sku])
    if args.sku not in products:
        print(f"{args.sku} is not in {settings['products_table']}",
              file=sys.stderr)
        return 1

    allowed = compute_band(lines, products, engine.store.rules())
    print(f"band            up to {allowed / 100:.2f}%")

    if args.discount is None:
        return 0

    result = engine.check(lines, args.discount)
    print(f"asked           {args.discount / 100:.2f}%")
    print(f"result          "
          f"{'APPROVED' if result['approved'] else 'REFUSED'}")
    print()
    for check in result["checks"]:
        print(f"  {check['status']:<15} {check['rule']:<15} {check['detail']}")
    engine.logger.flush()
    return 0 if result["approved"] else 2


class _Rollback(Exception):
    """Aborts the write probe without committing anything."""


# ----------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="commerce-policy",
        description="Pricing guardrail. The model proposes; this decides.")
    parser.add_argument("--config", default="policy.config.json")
    parser.add_argument("--version", action="version",
                        version=f"commerce-policy {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate = sub.add_parser("migrate", help="create the policy schema")
    migrate.add_argument("--storefront-role",
                         help="the role your website runs as; all access to "
                              "the policy schema is revoked from it")
    migrate.add_argument("--engine-role",
                         help="the role this engine runs as; granted SELECT")
    migrate.set_defaults(func=cmd_migrate)

    verify = sub.add_parser("verify", help="check the install end to end")
    verify.set_defaults(func=cmd_verify)

    set_cost = sub.add_parser("set-cost", help="set a cost and/or a floor")
    set_cost.add_argument("sku")
    set_cost.add_argument("--cost", type=int, help="in paise")
    set_cost.add_argument("--floor", type=int, help="in paise")
    set_cost.set_defaults(func=cmd_set_cost)

    check = sub.add_parser("check", help="show the band, optionally test one")
    check.add_argument("sku")
    check.add_argument("--qty", type=int, default=1)
    check.add_argument("--discount", type=int, help="in bps; 800 = 8%%")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)

    try:
        settings = load(args.config)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 1

    try:
        return args.func(args, settings)
    except (StoreError, psycopg.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
