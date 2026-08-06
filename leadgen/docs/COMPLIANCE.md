# Compliance

**This is not legal advice.** It is a description of the obligations this
system is built around and how it tries to help you meet them. Before running
outreach at volume, talk to a lawyer who knows marketing law in your
jurisdiction.

The short version: collecting publicly listed business information for B2B
outreach is lawful in the United States. It is not unconditional, and the
conditions that bite are mostly about what you do *after* you collect it.

---

## What this system collects

Only information the business has published about itself:

- Name, address, phone, hours, category, rating, and review count from mapping
  providers under their APIs' terms
- Email addresses, contact forms, and social links published on the business's
  own website
- Measurable technical facts about that website

It does **not** buy data brokers' files, scrape personal social media accounts,
guess email addresses by pattern (`firstname@domain`), query email-finding
services, or attempt to access anything behind authentication.

That boundary is deliberate. "Published by the business for customers to use" is
a defensible position. "Inferred, purchased, or extracted from a private
source" is a different conversation with a different risk profile.

---

## robots.txt

Honored by default (`LEADGEN_RESPECT_ROBOTS_TXT=true`). Behavior:

- A missing or malformed robots.txt is treated as **allowed** — the convention
  every major crawler follows.
- A `401` or `403` on robots.txt itself is treated as **disallowed**: the
  operator is signaling that automated clients are unwelcome.
- A disallowed site is skipped, recorded with outcome `skipped_robots`, and
  scored `UNKNOWN` with **no fabricated problems**. It is never reported as
  defective just because we chose not to look.

Turning this off is available and almost never wise. robots.txt governs
automated fetching, not what you may conclude from facts you legitimately
observe — but ignoring it is what gets an IP blocked, which kills the whole
pipeline.

## Rate limiting

Two limiters, both on by default:

- **Per-host**: 0.5 requests/second, no burst allowance. A local business site
  is often on shared hosting where a burst of concurrent requests is
  operationally indistinguishable from an attack.
- **Global**: 12 requests/second across everything.

At most 5 pages are fetched per business. This is prospect research, not a
crawl.

---

## Marketing law

### CAN-SPAM (commercial email)

Applies to every commercial email you send. Requirements:

- Accurate `From`, `Reply-To`, and routing information
- A subject line that is not deceptive
- Your **valid physical postal address** in every message
- A clear, working opt-out mechanism
- Opt-outs honored **within 10 business days**

Penalties run per message, so a single non-compliant blast to 5,000 addresses is
a catastrophic number, not a slap.

`compliance/policy.py:outreach_disclaimer()` generates a conforming footer.
Suppression is enforced by `filter_suppressed()` at every export and report
boundary, so a suppressed record cannot reach a channel where someone could act
on it.

**Note:** this system deliberately does not generate complete cold emails. It
produces an *angle* and talking points; a human writes the message. That is
partly quality — personalization is the entire value — and partly risk: a model
generating thousands of unreviewed commercial emails is how a sending domain
gets blacklisted.

### TCPA (calls and texts)

The expensive one. Statutory damages are $500-$1,500 **per message**, and it is
a favorite of class-action plaintiffs.

- Automated calls and texts to **mobile numbers** require prior express consent.
- Manually dialed calls to a **business line** are generally permitted.
- Sole proprietors routinely list a mobile number as their business number, so
  "it's a business line" is not a reliable defense.

This system never dials and never texts. It produces a list a human works. **Do
not wire it to an autodialer or an SMS platform without counsel.**

### Do-Not-Call registries

The National DNC Registry protects consumers, and business-to-business calls are
generally exempt. But sole proprietors blur that line, and several states run
their own registries with narrower exemptions. Scrub against the DNC registry
before any calling campaign.

### CCPA / CPRA (California)

Sole proprietors count as individuals under California privacy law, which means
a meaningful share of your leads may have rights here:

- Right to know what you hold and where it came from
- Right to deletion
- Right to opt out of "sale" or "sharing" — note that CCPA's definition of
  "sale" is broad and can cover disclosure to third parties for value

`compliance/policy.py:build_deletion_record()` produces the record you need to
service a request: what you hold, when you collected it, from where, and for
what purpose.

To delete someone fully:

```bash
leadgen suppress "their@email.com" --kind email --reason "CCPA deletion request"
# then remove the business row; the schema cascades to audits, leads, and insights
```

Suppress **and** delete. Suppression alone leaves the data in your database,
which does not satisfy a deletion request.

### GDPR

If you ever contact EU-based businesses, GDPR applies and is stricter: you need
a lawful basis (legitimate interest is arguable for B2B, but must be documented
and balanced), and data-subject rights are broader. This system is built for US
B2B prospecting; treat EU expansion as a separate compliance project.

---

## Provider terms

### Google Places / Maps

The terms are specific and worth reading rather than assuming:

- Place **IDs** may be cached indefinitely. Place **content** may be cached only
  to improve performance, with 30 days the widely used conservative refresh
  interval (`PLACES_CONTENT_MAX_CACHE_DAYS`).
- Content displayed to users generally requires attribution.
- Building a competing mapping or directory product from the data is
  prohibited.

Using it to research prospects for your own outreach is within normal use.
Republishing a scraped directory is not.

### OpenStreetMap

ODbL licensed. Free to use, including commercially. If you publish a derived
database, share-alike obligations attach. Internal prospect research does not
trigger that, but publishing your lead list would need care.

The public Overpass instance is a volunteer resource. This system sends one
query per cell with a server-side timeout. Past a few thousand queries a day,
run your own instance rather than turning up concurrency on theirs.

### PageSpeed Insights

Free with a key, 25,000 requests/day. No redistribution restrictions on the
scores.

---

## Practical guidance

**Do**

- Change `LEADGEN_USER_AGENT` to include a real contact URL. When a site
  operator wonders who is fetching their pages, they should be able to find out
  and reach you.
- Honor every opt-out immediately, in whatever channel it arrives. Suppress
  before the next run.
- Keep the audit evidence. If a prospect disputes a claim, you can show exactly
  what was measured and when.
- Delete leads you will never contact. A smaller database is a smaller breach.

**Don't**

- Wire this to an autodialer or bulk SMS without counsel
- Send email without a physical address and a working unsubscribe
- Buy supplementary contact data and merge it in without checking that source's
  provenance — you inherit its problems
- Ignore `robots.txt` because it is slowing you down
- Claim problems the audit did not actually observe. Every problem string in
  this system traces to a specific measurement; keep it that way, because the
  first time a prospect checks and you are wrong, you have lost them and your
  reputation in a small market.

---

## Suppression

```bash
leadgen suppress "someone@example.com" --kind email --reason "unsubscribed"
leadgen suppress "+16265551234"        --kind phone
leadgen suppress "example.com"         --kind domain
leadgen suppress "acme plumbing"       --kind business
```

Or via the API, which suppresses by every identifier held for that lead at once,
so the same business cannot reappear tomorrow through a different provider
record:

```bash
curl -X POST localhost:8000/api/leads/123/suppress \
  -H "X-API-Key: $LEADGEN_API_KEY"
```

Suppressions live in their own table rather than as a flag on the lead, so a
re-score can never resurrect an opt-out.
