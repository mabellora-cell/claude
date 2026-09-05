# Household expense split

Turns a month of raw statement exports into a ledger of shared expenses and a
single number: what your husband owes you.

```bash
python3 run.py --month 2026-08
```

Reads everything in `inbox/`, applies `rules.toml`, writes
`out/ledger-2026-08.csv` and `out/summary-2026-08.md`.

---

## What to upload

One folder per source. **CSV is what matters** — PDF statements can be read but
the numbers have to be re-typed, so always take the CSV when the site offers it.

| Put it in | Where to get it |
|---|---|
| `inbox/bofa/` | Accounts → the account → **Download** → *Microsoft Excel format (CSV)*. Do this for **both** the checking account and any BofA credit card. |
| `inbox/capitalone/` | Account → **View Statements** → *Download Transactions* → CSV. Pick a date range, not a statement period, so it lines up with the month. |
| `inbox/amex/` | Statements & Activity → **Download** → *CSV*. Tick **"Include all additional details"** — that's what carries the Amazon order number. |
| `inbox/paypal/` | Activity → **Download** → *CSV*, type **Balance affecting**. |
| `inbox/klarna/` | No real export. See [Klarna](#klarna) below. |
| `inbox/amazon/` | **Not the order page.** See [Amazon](#amazon) below — this is the one that takes 24 hours, so start it first. |

File names don't matter. Multiple files per folder are fine (e.g. two Amex
cards, or three months at once — `--month` filters by date afterward).

### Amazon

The card statement only ever says `AMAZON MKTPL*RT4YU1 $127.43`, which tells you
nothing. You need the item-level export:

1. [amazon.com/hz/privacy-central/data-requests/preview.html](https://www.amazon.com/hz/privacy-central/data-requests/preview.html)
2. Request **"Your Orders"** (not the full account dump — it's much slower).
3. Amazon emails a link in a few hours to ~24h. Download the zip.
4. Drop `Retail.OrderHistory.1.csv` into `inbox/amazon/`.

That file has one row per **item** with the product name and price, which is how
a single $127.43 charge gets resolved into "diapers + paper towels + face serum"
and split proportionally instead of guessed at.

Do this once and it covers your whole order history, so future months only need
a fresh export if you want the latest orders.

### Klarna

Klarna has no CSV export. Two options:

- **Best:** request your data at Klarna → Settings → Privacy → *Request my data*.
- **Fine:** screenshot the purchase list from the app and paste the purchases
  into a CSV with columns `Date,Description,Amount`.

Klarna matters less than it looks, because Klarna's installments already show up
on whichever card funds them. The Klarna export is only there to tell you *what
the purchase was* — see [double counting](#the-double-counting-problem).

---

## The double-counting problem

This is the part that quietly corrupts a hand-built ledger, so the tool handles
it explicitly.

The same dollar shows up in more than one export:

- **Paying your Amex bill from BofA checking.** The $1,250 payment is not an
  expense — the individual Amex charges already are. Counting both inflates the
  month by the full card balance.
- **PayPal and Klarna.** A PayPal charge funded by your Amex appears in *both*
  the PayPal export and the Amex export.
- **Transfers between your own accounts.**

The rules:

- Bank and card exports are the **money** — they're what actually left an account.
- PayPal, Klarna and Amazon are **detail** — they explain a charge that's already
  counted.
- When a PayPal/Klarna row matches a card charge (same amount, within 3 days),
  the card row is kept, the merchant name is copied over from the wallet export,
  and the wallet row is marked a duplicate.
- When a PayPal row matches *nothing* — you paid from PayPal balance or a linked
  bank account — it's real money out and gets counted on its own.
- Card payments and transfers are excluded by rule.

Every excluded and deduplicated row stays visible in the ledger CSV with a reason,
so you can audit it rather than trust it.

---

## Nothing is silently split

Anything no rule matches is marked **`review`** and listed at the top of the
summary, with its Amazon items spelled out. Review rows are *excluded from the
total* — the number you send your husband only ever contains transactions that
were classified deliberately.

To resolve them:

1. Open `out/ledger-2026-08.csv`.
2. Change the `split` column to `shared`, `personal`, or `excluded`.
3. Recompute from your decisions:

```bash
python3 run.py --month 2026-08 --ledger out/ledger-2026-08.csv
```

4. Then teach `rules.toml` the merchants you just decided, so next month is quieter.

The month-to-month goal is a shrinking review list.

---

## rules.toml

The only file you maintain. Merchant patterns (regex, case-insensitive) map to
`shared`, `personal`, or `excluded`.

```toml
[[rule]]
name = "groceries"
split = "shared"
category = "Groceries"
match = ["whole ?foods", "heb", "trader joe", "costco"]
```

`[[item_rule]]` blocks match Amazon **product names** rather than merchants,
which is what makes a mixed order split correctly.

Two behaviours worth knowing:

- **Most specific match wins**, not first-listed. `LITTLE SPROUTS PRESCHOOL`
  contains the grocery keyword `sprouts`, but `little sprouts` and `preschool`
  are longer matches, so it lands in Baby. Rule order in the file doesn't matter.
- Patterns are word-anchored but tolerate plurals, so `diaper` matches `Diapers`
  while `heb` does not match `the best`.

Not everything is 50/50 — add `share = 0.25` to a rule to override.

### Uneven splits

`share` is the fraction *he* owes. For a mixed Amazon order it's computed
automatically: if $68.43 of a $127.43 charge is shared, he owes 50% of that
portion, so `share` becomes 0.2685 and the ledger shows $34.21.

---

## Output

**`out/ledger-<month>.csv`** — every transaction from every source, with `split`,
`share`, `owed`, the matched `rule`, Amazon item detail, and dedup reasons. This
is the auditable record and the file you edit.

**`out/summary-<month>.md`** — the settlement: net owed, review queue, category
breakdown, and shared line items. This is the one you send him.

Money he's already sent you (Zelle/Venmo, matched via `partner_aliases` in
`rules.toml`) is netted off automatically, so the final number is what's actually
outstanding — not the gross half.

---

## Privacy

`inbox/` and `out/` are gitignored. Real statements never get committed.

---

## Tests

```bash
python3 tests/test_split.py
```

Runs the full pipeline over `tests/fixtures/` — fake statements covering the
awkward cases: a card payment, a refund, a wallet duplicate, a cancelled Amazon
order, a mixed Amazon order, and a merchant whose name collides with another
rule's keyword.
