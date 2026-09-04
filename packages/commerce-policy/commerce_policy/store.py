"""The merchant's own data, read from the merchant's own database.

Two tables live in a `policy` schema that their storefront role has no grant
on. That is the whole security model: their website cannot run SELECT on a
cost column, so no bug in their website — an injected query, a careless API
route, a debug endpoint left open — can leak one. The protection is a missing
grant, not a promise about code.

Prices and stock come from their existing products table, because those are
already public: anyone can open the shop and read them.
"""

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

STATEMENT_TIMEOUT_MS = 5000

# The columns that go into the hash, in one place, because record() and
# verify_chain() must agree exactly or every audit reads as tampered with.
CHAINED_FIELDS = ("merchant_id", "sku", "asked_bps", "allowed_bps",
                  "result", "failed_rules", "engine_version")


class StoreError(RuntimeError):
    pass


class PolicyStore:
    def __init__(self, settings):
        self.settings = settings

    def _connect(self):
        """A connection that cannot write and cannot hang.

        default_transaction_read_only makes the server refuse writes even if
        the role was granted more than it should have been. The statement
        timeout stops a slow query from holding a checkout open.

        Decision records are the one exception and open their own connection.
        """
        return psycopg.connect(
            self.settings["database_url"], row_factory=dict_row,
            connect_timeout=10,
            options=f"-c default_transaction_read_only=on "
                    f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")

    def _to_paise(self, value):
        if value is None:
            return None
        if self.settings["price_is_minor_units"]:
            return int(value)
        return int(round(float(value) * 100))

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def products(self, skus):
        """Price and stock from the storefront, cost and floor from policy.

        One query per source, joined here rather than in SQL, so a shop whose
        products live in a differently named table still works by changing
        three strings in the config.
        """
        skus = list(skus)
        if not skus:
            return {}

        s = self.settings
        public_sql = sql.SQL(
            "SELECT {sku} AS sku, {price} AS price, {stock} AS stock "
            "FROM {table} WHERE {sku} = ANY(%s)").format(
                sku=sql.Identifier(s["sku_column"]),
                price=sql.Identifier(s["price_column"]),
                stock=sql.Identifier(s["stock_column"]),
                table=sql.Identifier(s["products_table"]))

        with self._connect() as conn:
            public = conn.execute(public_sql, (skus,)).fetchall()
            economics = conn.execute(
                "SELECT sku, cost_paise, floor_price_paise "
                "FROM policy.economics WHERE sku = ANY(%s)",
                (skus,)).fetchall()

        by_sku = {}
        for row in public:
            by_sku[str(row["sku"])] = {
                "price_paise": self._to_paise(row["price"]),
                "stock": int(row["stock"] or 0),
                "cost_paise": None,
                "floor_price_paise": None,
            }
        for row in economics:
            product = by_sku.get(str(row["sku"]))
            if product is not None:
                product["cost_paise"] = row["cost_paise"]
                product["floor_price_paise"] = row["floor_price_paise"]

        return by_sku

    def rules(self):
        """Limits from policy.rules, falling back to the config file.

        The table wins when it has a row, so an operator can tighten a cap
        without a redeploy. A shop that never populates it runs on the config,
        which is what a first install looks like.
        """
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT max_discount_bps, min_margin_bps "
                    "FROM policy.rules WHERE merchant_id = %s",
                    (self.settings["merchant_id"],)).fetchone()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise StoreError(
                "this role cannot read policy.rules. Grant USAGE on schema "
                "policy and SELECT on policy.rules to the engine role.") from exc
        except psycopg.errors.UndefinedTable as exc:
            raise StoreError(
                "the policy schema is missing. Run: commerce-policy migrate"
            ) from exc

        if row is None:
            return {"max_discount_bps": self.settings["max_discount_bps"],
                    "min_margin_bps": self.settings["min_margin_bps"]}
        return {"max_discount_bps": row["max_discount_bps"],
                "min_margin_bps": row["min_margin_bps"]}

    # ------------------------------------------------------------------
    # writing the one thing this package writes
    # ------------------------------------------------------------------

    def record(self, decision):
        """Append one hash-chained decision to policy.decisions.

        Chained so that an edit is detectable, and kept on the merchant's own
        server so that their record of what was approved outlives their
        relationship with us. The control plane receives a copy of the safe
        fields; this table is the original.

        Returns the hash, or None if the record could not be written — a
        failure to log must never fail a sale.
        """
        import hashlib
        import json

        # Hash exactly the columns the table stores, and nothing else. If this
        # serialised the whole decision dict, adding one field anywhere in the
        # package would break every existing link in the chain — and it would
        # look like tampering.
        payload = json.dumps({k: decision[k] for k in CHAINED_FIELDS},
                             sort_keys=True, separators=(",", ":"),
                             default=str)
        try:
            with psycopg.connect(self.settings["database_url"],
                                 row_factory=dict_row,
                                 connect_timeout=10) as conn:
                with conn.transaction():
                    previous = conn.execute(
                        "SELECT hash FROM policy.decisions "
                        "ORDER BY seq DESC LIMIT 1").fetchone()
                    prev_hash = previous["hash"] if previous else "0" * 64
                    digest = hashlib.sha256(
                        f"{prev_hash}|{payload}".encode()).hexdigest()
                    conn.execute(
                        "INSERT INTO policy.decisions "
                        "(merchant_id, sku, asked_bps, allowed_bps, result, "
                        " failed_rules, engine_version, prev_hash, hash) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (decision["merchant_id"], decision["sku"],
                         decision["asked_bps"], decision["allowed_bps"],
                         decision["result"], decision["failed_rules"],
                         decision["engine_version"], prev_hash, digest))
            return digest
        except psycopg.Error:
            return None

    def verify_chain(self):
        """Recompute every link. Returns (ok, first_broken_seq)."""
        import hashlib
        import json

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT seq, merchant_id, sku, asked_bps, allowed_bps, "
                "result, failed_rules, engine_version, prev_hash, hash "
                "FROM policy.decisions ORDER BY seq").fetchall()

        prev_hash = "0" * 64
        for row in rows:
            payload = json.dumps({k: row[k] for k in CHAINED_FIELDS},
                                 sort_keys=True, separators=(",", ":"),
                                 default=str)
            expected = hashlib.sha256(
                f"{prev_hash}|{payload}".encode()).hexdigest()
            if expected != row["hash"] or row["prev_hash"] != prev_hash:
                return False, row["seq"]
            prev_hash = row["hash"]
        return True, None
