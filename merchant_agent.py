"""The merchant's own agent: what to do next, from their own data.

The model does exactly one job here — decide which function answers the
question. It does not write the answer. Every figure a merchant reads comes
out of `growth.py`, which reads it out of a row.

That split is the whole design. A model asked to *summarise* sales data will
eventually produce a plausible number that came from nowhere, and an owner who
raises a discount cap on the strength of it has been harmed by us. A model
asked only to *route* can be wrong in one visible way: it picks the wrong
function, and the merchant sees an answer to a question they did not ask.

Scope follows from the same structure. There is no tool that takes a merchant
name, a person, or a topic, so "what does the shop next door charge" and "how
do I make chicken curry" have nothing to call. They are not refused by an
instruction that could be argued with; they are refused because the system
contains no way to answer them.
"""

import json
import logging

import config
import db
import events
import growth
import policy_log

log = logging.getLogger("merchant_agent")

REFUSAL = (
    "I only answer questions about your own shop — what is selling, what "
    "shoppers are asking for, and what is blocking sales. I have no way to "
    "look up anything else.")

TOOLS = [
    {"type": "function", "function": {
        "name": "growth_plan",
        "description": "What this merchant should do next to sell more, "
                       "ranked by rupees. Use for any open question about "
                       "growing, improving, or 'what should I do'.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "Days to look back. Default 30."}}}}},

    {"type": "function", "function": {
        "name": "unmet_demand",
        "description": "What shoppers searched for and found nothing. Use for "
                       "questions about what to stock, what people want, or "
                       "what is missing from the catalog.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}}}},

    {"type": "function", "function": {
        "name": "blocked_sales",
        "description": "Which policy rule refused the most sales, and how "
                       "often. Use for questions about refused, blocked, or "
                       "lost sales, or about discount limits.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}}}},

    {"type": "function", "function": {
        "name": "product_performance",
        "description": "Which individual products are selling and which are "
                       "not, with units and revenue. Use for any question "
                       "naming products: what sells more, what sells less, "
                       "best seller, worst seller, what is not moving.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}}}},

    {"type": "function", "function": {
        "name": "shop_activity",
        "description": "Plain counts: searches, offers shown, checkouts "
                       "started, conversion. Use when asked how the shop is "
                       "doing, or for numbers rather than advice.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}}}}},

    {"type": "function", "function": {
        "name": "integration_prompt",
        "description": "The ready-to-paste prompt that wires this system into "
                       "the merchant's website. Use when they ask to "
                       "integrate, install, connect, or set up.",
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string",
                     "description": "Their coding tool: Claude Code, Cursor, "
                                    "Lovable, Replit, VS Code, Codex, "
                                    "Emergent or Antigravity."}}}}},

    {"type": "function", "function": {
        "name": "out_of_scope",
        "description": "The question is not about this merchant's own shop — "
                       "another business, a person, or any general topic. "
                       "Pick this rather than guessing.",
        "parameters": {"type": "object", "properties": {}}}},
]

# Used when no model is configured or the model call fails. Deliberately
# crude: a fallback, not a second implementation.
#
# Anything it cannot place goes to out_of_scope. An earlier version defaulted
# to growth_plan on the reasoning that it is the question most owners are
# really asking — which meant that with the model down, "how do I make chicken
# curry" was answered with a growth plan. Both failures are possible and only
# one is acceptable: refusing a real question costs a rephrase, while
# answering an unrelated one breaks the promise this agent is built on.
KEYWORDS = (
    ("integration_prompt", ("integrat", "install", "set up", "setup",
                            "connect", "embed", "widget", "prompt for",
                            "paste", "wire")),
    ("unmet_demand", ("stock", "searching", "searched", "looking for",
                      "missing", "don't have", "dont have", "demand",
                      "should i add", "what to sell")),
    ("blocked_sales", ("refus", "blocked", "reject", "lost sale", "cap",
                       "discount", "margin", "policy", "limit")),
    ("product_performance", ("selling more", "selling less", "sells more",
                             "sells least", "best seller", "worst seller",
                             "top selling", "which product", "not moving",
                             "not selling", "moving slow")),
    ("shop_activity", ("how many", "conversion", "traffic", "numbers",
                       "activity", "how is my", "how's my", "doing")),
    ("growth_plan", ("grow", "improve", "more sales", "sell more",
                     "what should", "advice", "advise", "recommend",
                     "increase", "revenue", "business", "profit")),
)


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def _route_with_model(question):
    """Ask DeepSeek which function answers this. Returns (name, args) or None.

    tool_choice is forced, so the model cannot reply with prose. The only
    thing it may return is a choice from the list above — which is why a
    jailbreak in the question has nothing to reach: there is no free-text
    channel out of this call.
    """
    if not config.DEEPSEEK_API_KEY:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        client = OpenAI(api_key=config.DEEPSEEK_API_KEY,
                        base_url=config.DEEPSEEK_BASE_URL, timeout=15.0)
        response = client.chat.completions.create(
            model=config.DEEPSEEK_MODEL,
            max_tokens=200,
            tools=TOOLS,
            tool_choice="required",
            messages=[
                {"role": "system", "content":
                    "Route the merchant's question to exactly one function. "
                    "You answer nothing yourself. If the question is not "
                    "about this merchant's own shop, choose out_of_scope."},
                {"role": "user", "content": question[:1000]},
            ])
        calls = response.choices[0].message.tool_calls
        if not calls:
            return None
        call = calls[0]
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        return call.function.name, args
    except Exception as exc:                              # noqa: BLE001
        log.warning("routing call failed (%s: %s); using keywords",
                    type(exc).__name__, exc)
        return None


def _route_with_keywords(question):
    lowered = (question or "").lower()
    for name, triggers in KEYWORDS:
        if any(t in lowered for t in triggers):
            return name, {}
    return "out_of_scope", {}


# --------------------------------------------------------------------------
# answering
# --------------------------------------------------------------------------

def ask(merchant_id, question):
    """Route, run, render. Returns a dict the UI can display directly.

    The rendering is templated rather than generated. A second model call to
    "write this up nicely" is exactly where an invented number would enter,
    and the findings already carry sentences written against real rows.
    """
    question = (question or "").strip()
    if not question:
        return _answer("out_of_scope", REFUSAL)

    routed = _route_with_model(question)
    source = "deepseek"
    if routed is None:
        routed = _route_with_keywords(question)
        source = "keywords"

    name, args = routed
    days = _days(args.get("days"))

    # Which questions merchants actually ask, and whether the router placed
    # them. A run of out_of_scope on real questions is the signal that the
    # tool list is missing something — which is exactly how the gap around
    # "which product is selling more" was found.
    events.record(merchant_id, "merchant_question", query=question,
                  tool=name, router=source)

    handler = HANDLERS.get(name)
    if handler is None:
        log.warning("model chose unknown tool %r", name)
        return _answer("out_of_scope", REFUSAL, source=source)

    try:
        answer = handler(merchant_id, days, args)
    except Exception as exc:                              # noqa: BLE001
        log.exception("tool %s failed", name)
        return _answer(name, "Something went wrong reading your data. "
                             "Nothing was changed.", source=source, ok=False)

    answer["source"] = source
    return answer


def _days(value):
    try:
        return max(1, min(int(value), 365))
    except (TypeError, ValueError):
        return 30


def _answer(tool, text, source="keywords", data=None, ok=True):
    return {"tool": tool, "text": text, "data": data or {}, "ok": ok,
            "source": source}


def _rupees(paise):
    return f"Rs {paise / 100:,.0f}"


# --------------------------------------------------------------------------
# the handlers. every sentence below is assembled from a queried row.
# --------------------------------------------------------------------------

def _growth_plan(merchant_id, days, args):
    result = growth.plan(merchant_id, days)

    if not result["enough_data"]:
        return _answer("growth_plan", result["note"], data=result)

    if not result["findings"]:
        return _answer(
            "growth_plan",
            f"Nothing is obviously costing you money right now. "
            f"{result['searches_seen']} searches and "
            f"{result['decisions_seen']} pricing decisions in the last "
            f"{days} days, and no rule is blocking enough sales to be worth "
            f"changing.", data=result)

    priced = result["impact_priced"]
    head = (f"{len(result['findings'])} things to fix, worth about "
            f"{_rupees(result['total_impact_paise'])} together:"
            if priced else
            f"{len(result['findings'])} things to fix. No confirmed orders "
            f"yet, so I cannot put a rupee figure on them:")

    lines = [head, ""]
    for i, finding in enumerate(result["findings"], 1):
        lines += [f"{i}. {finding['headline']}",
                  f"   {finding['evidence']}",
                  f"   → {finding['action']}"]
        if priced:
            lines.append(f"   Worth about {_rupees(finding['impact_paise'])}.")
        lines.append("")

    return _answer("growth_plan", "\n".join(lines).strip(), data=result)


def _unmet_demand(merchant_id, days, args):
    demand = events.unmet_demand(merchant_id, days, limit=10)
    if not demand:
        return _answer(
            "unmet_demand",
            f"No repeated empty searches in the last {days} days. Either "
            f"your catalog covers what people ask for, or there is not "
            f"enough traffic yet to tell.")

    total = sum(d["times"] for d in demand)
    lines = [f"{total} shoppers searched for things you do not stock:", ""]
    lines += [f"  {d['times']:>3}x  {d['asked']}" for d in demand]
    lines += ["", f"Stocking something for \"{demand[0]['asked']}\" would "
                  f"reach {demand[0]['times']} of them."]
    return _answer("unmet_demand", "\n".join(lines),
                   data={"unmet_demand": demand})


def _blocked_sales(merchant_id, days, args):
    report = policy_log.summary(merchant_id, days)
    if not report["decisions"]:
        return _answer(
            "blocked_sales",
            f"No pricing decisions recorded in the last {days} days. Your "
            f"policy engine reports here once it is installed and running.")

    if not report["refused"]:
        return _answer(
            "blocked_sales",
            f"{report['decisions']} pricing decisions, none refused. Your "
            f"limits are not costing you sales.", data=report)

    lines = [f"{report['refused']} of {report['decisions']} offers were "
             f"refused ({report['refusal_rate'] * 100:.0f}%):", ""]
    lines += [f"  {r['refusals']:>3}x  {r['rule']}" for r in report["by_rule"]]
    lines += ["", f"\"{report['top_refusal']}\" is what is binding. Ask me "
                  f"how to grow and I will tell you whether it should be."]
    return _answer("blocked_sales", "\n".join(lines), data=report)


def _product_performance(merchant_id, days, args):
    report = growth.product_performance(merchant_id, max(days, 90))

    if not report["products_sold"]:
        never = report["never_sold"]
        if not never:
            return _answer("product_performance",
                           "Nothing has sold yet, and there are no active "
                           "products in stock to report on.")
        lines = [f"Nothing has sold in the last {report['days']} days. "
                 f"{len(never)} products are in stock and waiting:", ""]
        lines += [f"  {n['name']} — {n['stock']} in stock at "
                  f"{_rupees(n['price_paise'])}" for n in never[:5]]
        return _answer("product_performance", "\n".join(lines), data=report)

    lines = [f"Last {report['days']} days — {report['units_total']} units, "
             f"about {_rupees(report['revenue_paise'])} (estimated):", "",
             "Selling most:"]
    lines += [f"  {b['units']:>3} x  {b['name']} — "
              f"{_rupees(b['revenue_paise'] or 0)}"
              for b in report["best"][:5]]

    if report["worst"] and report["products_sold"] > 1:
        lines += ["", "Selling least:"]
        lines += [f"  {w['units']:>3} x  {w['name']}"
                  for w in report["worst"][:3]]

    if report["never_sold"]:
        lines += ["", f"Not sold at all ({len(report['never_sold'])}):"]
        lines += [f"  {n['name']} — {n['stock']} in stock"
                  for n in report["never_sold"][:5]]

    lines += ["", "Revenue is estimated: line-item prices are not stored, so "
                  "an order's discount is spread across its products."]
    return _answer("product_performance", "\n".join(lines), data=report)


def _shop_activity(merchant_id, days, args):
    summary = events.summary(merchant_id, days)
    if not summary["searches"]:
        return _answer(
            "shop_activity",
            f"No shopper activity in the last {days} days. Searches appear "
            f"here once the widget is on your site.")

    lines = [f"Last {days} days:", "",
             f"  {summary['searches']} searches",
             f"  {summary['searches_with_no_results']} found nothing"]
    if summary["proposals"]:
        lines.append(f"  {summary['proposals']} offers shown")
        lines.append(f"  {summary['purchases_started']} reached checkout")
        if summary["conversion"] is not None:
            lines.append(f"  {summary['conversion'] * 100:.0f}% conversion")
    if summary["median_search_ms"]:
        lines.append(f"  {summary['median_search_ms']}ms median search")
    return _answer("shop_activity", "\n".join(lines), data=summary)


def merchant_limits(merchant_id):
    row = db.query_one(
        "SELECT max_discount_bps, min_margin_bps FROM merchants WHERE id = %s",
        (merchant_id,))
    return dict(row) if row else None


def build_prompt(merchant_id, tool, browse_key=None):
    """Assemble the prompt, with the merchant's real limits in it."""
    tool = (tool or "your coding tool").strip()[:40]
    return {
        "tool": tool,
        "merchant_id": merchant_id,
        "key_included": bool(browse_key),
        "prompt": _prompt_text(merchant_id, tool, browse_key,
                               merchant_limits(merchant_id)),
    }


def _integration_prompt(merchant_id, days, args):
    tool = (args.get("tool") or "your coding tool").strip()[:40]
    data = build_prompt(merchant_id, tool)
    return _answer(
        "integration_prompt",
        f"Here is the prompt for {tool}. The browse key is left blank — keys "
        f"are shown once and cannot be read back. Use the button on the tool "
        f"picker to mint a fresh one written straight into the prompt.",
        data=data)


ENGINE_VERSION = "1.2.0"


PLACEHOLDER = "<from dashboard.razorpay.com/app/keys>"


def _razorpay_lines():
    """The Razorpay credentials to print, and a line explaining them.

    Both are filled in from the environment, and only when the key id is a
    test one. That gate is the whole safety of this: a live key id names an
    account taking real money and a live secret signs payments against it, so
    neither may ever render on a page anyone can open. Test-mode credentials
    cannot move real money, which is what makes sharing a set for a demo a
    reasonable trade rather than a mistake.

    Read from config at render time rather than written into this file, so the
    secret exists in the server's environment and in the rendered page but
    never in the repository. A secret committed to source lives in the history
    after it is removed; one in an environment variable is replaced by
    changing a setting.

    Whoever runs this still owes themselves a rotation once the demo is over:
    every visitor who opened the page has the key.
    """
    key_id = (config.RAZORPAY_KEY_ID or "").strip()
    secret = (config.RAZORPAY_KEY_SECRET or "").strip()

    if not key_id.startswith("rzp_test_"):
        return PLACEHOLDER, PLACEHOLDER, ""

    note = ("\nBoth Razorpay values above are a shared TEST account, so "
            "checkout runs end to\nend with nothing for you to configure. "
            "Test mode cannot move real money.\nReplace both with your own "
            "keys before taking real payments.\n")
    return key_id, (secret or PLACEHOLDER), note


def _prompt_text(merchant_id, tool, browse_key=None, limits=None):
    """The prompt a merchant pastes into their coding tool.

    browse_key is written in only when the caller has just minted one. Keys
    are displayed once and never recoverable, so every other caller gets a
    placeholder — a prompt that quietly carried a wrong key would fail at
    runtime, in their codebase, with no indication of why.

    Razorpay's own keys stay placeholders regardless. They belong to the
    merchant's Razorpay account, not to us, and we have never held them.
    """
    limits = limits or {"max_discount_bps": 1000, "min_margin_bps": 2000}
    key_line = (f"COMMERCE_POLICY_API_KEY={browse_key}" if browse_key
                else "COMMERCE_POLICY_API_KEY=<paste your browse key>")
    razorpay_id, razorpay_secret, razorpay_note = _razorpay_lines()

    return f"""Integrate the Commerce Control Plane into this codebase.

=== CREDENTIALS ===

MERCHANT_ID: {merchant_id}
CONTROL_PLANE=https://commerce-control-plane-api.vercel.app
{key_line}

POLICY_DB_URL=<your postgres connection string>
RAZORPAY_KEY_ID={razorpay_id}
RAZORPAY_KEY_SECRET={razorpay_secret}
{razorpay_note}
Put every line above in .env. Never commit it, and never put the
COMMERCE_POLICY_API_KEY or the Razorpay secret in front-end code.

=== INSTALL ===

pip install "commerce-policy @ git+https://github.com/manitejakanuri1/commerce-control-plane@main#subdirectory=packages/commerce-policy"

The package is not on PyPI. It installs from the repository, and the pin
above is the {ENGINE_VERSION} release this control plane accepts.

Create policy.config.json in the project root:

{{
  "merchant_id": "{merchant_id}",
  "max_discount_bps": {limits['max_discount_bps']},
  "min_margin_bps": {limits['min_margin_bps']},
  "products_table": "products",
  "sku_column": "sku",
  "price_column": "price",
  "stock_column": "stock",
  "cost_column": "cost"
}}

Adjust the four column names to match this project's own schema.

=== TASK ===

Detect the project stack and implement agentic search with:

1. Run: commerce-policy migrate --storefront-role <this app's database role>
   This revokes the policy schema from the web app's role, so no bug in the
   site can read a cost.

2. Backend endpoint POST /agent/search
   Forward the shopper's text and the sku list to
   {{CONTROL_PLANE}}/v1/catalog/search with the X-API-Key header.
   Return the products it sends back.

3. Backend endpoint POST /agent/offer
   Call engine.band(cart) then engine.check(cart, discount_bps) from
   commerce-policy. Only if check() returns approved, create the Razorpay
   order for result["net_paise"].

4. Backend endpoint POST /agent/webhook
   Verify the Razorpay signature against the raw request body, then mark the
   order paid. Do not trust a parsed payload.

5. Frontend: attach to the existing search input. Queries of four words or
   more open the agent panel; shorter ones fall through to the site's own
   search unchanged.

=== RULES ===

Do not write margin, discount or price-floor logic. commerce-policy already
does it, and a second implementation will disagree with the first.

Do not send cost or customer data to the control plane. It never needs them.

If the control plane is slow or unreachable, remove the panel and let the
site's own search handle the query. The shop must not break because we did."""


def _out_of_scope(merchant_id, days, args):
    return _answer("out_of_scope", REFUSAL)


HANDLERS = {
    "growth_plan": _growth_plan,
    "unmet_demand": _unmet_demand,
    "blocked_sales": _blocked_sales,
    "product_performance": _product_performance,
    "shop_activity": _shop_activity,
    "integration_prompt": _integration_prompt,
    "out_of_scope": _out_of_scope,
}
