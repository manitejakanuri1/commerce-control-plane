"""Security review of the live database.

Supabase publishes every table in the `public` schema through PostgREST, and
the publishable key that reaches it is public by design. Row Level Security is
what stands between that endpoint and the data. A table with RLS disabled is
readable by anyone holding a key that was never meant to be secret.
"""

import db

print("=" * 66)
print("ROW LEVEL SECURITY")
print("=" * 66)

rows = db.query("""
    SELECT c.relname AS table_name,
           c.relrowsecurity  AS rls_enabled,
           c.relforcerowsecurity AS rls_forced,
           (SELECT COUNT(*) FROM pg_policies p
             WHERE p.schemaname = 'public' AND p.tablename = c.relname)
             AS policy_count
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
    ORDER BY c.relname
""")

exposed = []
for row in rows:
    state = "ON " if row["rls_enabled"] else "OFF"
    flag = "" if row["rls_enabled"] else "   <-- exposed via PostgREST"
    if not row["rls_enabled"]:
        exposed.append(row["table_name"])
    print(f"  {row['table_name']:<20} RLS {state}  "
          f"policies={row['policy_count']}{flag}")

print()
print("=" * 66)
print("WHAT AN ANONYMOUS CALLER COULD READ TODAY")
print("=" * 66)

sensitive = {
    "merchants": "api_key_hash, discount and margin limits",
    "orders": "buyer identifiers, amounts, payment ids",
    "products": "cost_paise - the merchant's buying price",
    "audit": "every policy decision and price ever computed",
    "webhook_events": "raw provider payloads",
    "reservations": "live stock holds",
}
for table in exposed:
    if table in sensitive:
        print(f"  {table:<20} {sensitive[table]}")

print()
print("=" * 66)
print("GRANTS HELD BY THE SUPABASE API ROLES")
print("=" * 66)

grants = db.query("""
    SELECT grantee, table_name,
           string_agg(DISTINCT privilege_type, ', ' ORDER BY privilege_type)
             AS privileges
    FROM information_schema.role_table_grants
    WHERE table_schema = 'public'
      AND grantee IN ('anon', 'authenticated')
    GROUP BY grantee, table_name
    ORDER BY grantee, table_name
""")
if grants:
    for row in grants:
        print(f"  {row['grantee']:<15} {row['table_name']:<20} "
              f"{row['privileges']}")
else:
    print("  none - the API roles hold no privileges on these tables")

print()
print("=" * 66)
print("OTHER CHECKS")
print("=" * 66)

secret = db.query_one(
    "SELECT COUNT(*) AS n FROM products WHERE cost_paise > price_paise")
print(f"  products priced below cost      : {secret['n']}")

neg = db.query_one("SELECT COUNT(*) AS n FROM products WHERE stock < 0")
print(f"  negative stock rows             : {neg['n']}")

trig = db.query_one("""
    SELECT COUNT(*) AS n FROM pg_trigger
    WHERE tgrelid = 'public.audit'::regclass AND NOT tgisinternal
""")
print(f"  audit append-only triggers      : {trig['n']}")

plain = db.query_one(
    "SELECT COUNT(*) AS n FROM merchants WHERE api_key_hash !~ '^[0-9a-f]{64}$'")
print(f"  merchant keys not sha256-hashed : {plain['n']}")

db.close()
