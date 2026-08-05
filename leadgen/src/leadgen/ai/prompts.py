"""Prompt construction for AI enrichment.

Design rules these prompts follow, learned the hard way:

* **Feed facts, forbid invention.** The model gets the audit's observed signals
  and nothing else. A hallucinated claim ("your site has no SSL") that turns
  out to be false destroys credibility on the first call, and the prospect
  will check.
* **Ask for the sales artifact, not an essay.** The useful output is a one-line
  hook and three concrete fixes, because that is what gets pasted into an
  email.
* **Constrain length in the schema, not the prose.** "Be concise" is ignored;
  a ``max_length`` in the tool schema is not.
* **Never write the outreach message itself.** The model produces an *angle*
  and talking points. A human writes the email. Auto-generated cold email at
  volume is how a domain gets blacklisted, and the personalization is the
  entire value of this system.
"""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT = """You are a web strategy analyst working for an independent \
website design studio. You review small local businesses and explain, in plain \
language a busy owner would understand, whether their web presence is costing \
them customers.

Rules you must follow:
- Use ONLY the observations provided. Never invent facts about the business or \
its website. If something was not observed, do not mention it.
- Write for the business owner, not for a developer. "Your site takes 6 seconds \
to load on a phone" — not "LCP exceeds threshold".
- Be specific and concrete. "Add a quote request form to the homepage" beats \
"improve conversion optimization".
- Be honest. If the site is genuinely fine, say so and score the opportunity low. \
A padded pitch wastes everyone's time.
- Never write a complete sales email or make claims about results, pricing, \
or guarantees."""


ENRICHMENT_TOOL: dict[str, Any] = {
    "name": "record_analysis",
    "description": "Record the structured analysis of this business as a website design prospect.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "maxLength": 320,
                "description": (
                    "Two sentences describing what this business does and who it serves, "
                    "based on its category, name, and any website content provided."
                ),
            },
            "opportunity": {
                "type": "string",
                "maxLength": 400,
                "description": (
                    "The single clearest reason this business would benefit from a new or "
                    "improved website, stated as the business impact rather than the technical "
                    "defect. Cite a specific observation."
                ),
            },
            "redesign_recommendations": {
                "type": "array",
                "minItems": 2,
                "maxItems": 5,
                "items": {"type": "string", "maxLength": 160},
                "description": (
                    "Concrete, prioritized changes. Each must tie to an observed problem."
                ),
            },
            "outreach_angle": {
                "type": "string",
                "maxLength": 300,
                "description": (
                    "The hook to open a cold call or email with — the one observation most "
                    "likely to make this specific owner stop and listen. Not a full message."
                ),
            },
            "outreach_subject": {
                "type": "string",
                "maxLength": 80,
                "description": (
                    "A short, specific, non-salesy email subject line referencing something "
                    "real about their business or site."
                ),
            },
            "talking_points": {
                "type": "array",
                "minItems": 2,
                "maxItems": 4,
                "items": {"type": "string", "maxLength": 140},
                "description": "Bullets for a phone call, in the order to raise them.",
            },
            "inferred_niche": {
                "type": "string",
                "maxLength": 60,
                "description": "Best-fit business category if the assigned one looks wrong; "
                "otherwise repeat the assigned niche.",
            },
            "estimated_value_tier": {
                "type": "string",
                "enum": ["low", "medium", "high", "premium"],
                "description": (
                    "Likely project value: low (<$1.5k), medium ($1.5-4k), high ($4-10k), "
                    "premium ($10k+). Base this on business scale and niche, not on how bad "
                    "the current site is."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Your confidence in this analysis given how much was observed.",
            },
        },
        "required": [
            "summary",
            "opportunity",
            "redesign_recommendations",
            "outreach_angle",
            "outreach_subject",
            "talking_points",
            "estimated_value_tier",
            "confidence",
        ],
    },
}


def build_enrichment_prompt(
    *,
    name: str,
    niche_label: str,
    niche_expectation: str,
    city: str | None,
    state: str | None,
    categories: list[str],
    rating: float | None,
    review_count: int | None,
    website: str | None,
    website_status: str,
    website_score: float | None,
    problems: list[str],
    tech_stack: list[str],
    design_era: str | None,
    page_title: str | None,
    meta_description: str | None,
    content_excerpt: str | None,
    has_contact_form: bool,
    has_phone: bool,
    hours_published: bool,
) -> str:
    """Assemble the observation sheet the model reasons over."""
    location = ", ".join(part for part in (city, state) if part) or "unknown location"

    lines = [
        "## Business",
        f"Name: {name}",
        f"Category: {niche_label}",
        f"Location: {location}",
    ]
    if categories:
        lines.append(f"Listed under: {', '.join(categories[:6])}")
    if rating is not None:
        lines.append(f"Google rating: {rating} from {review_count or 0} reviews")
    elif review_count:
        lines.append(f"Reviews: {review_count}")
    lines.append(f"Phone listed publicly: {'yes' if has_phone else 'no'}")
    lines.append(f"Hours published: {'yes' if hours_published else 'no'}")

    lines.append("")
    lines.append("## Web presence")
    if not website:
        lines.append(
            "NO WEBSITE FOUND. This business has no website listed anywhere we searched."
        )
    else:
        lines.append(f"Website: {website}")
        lines.append(f"Assessed status: {website_status}")
        if website_score is not None:
            lines.append(f"Quality score: {website_score:.0f} out of 100 (higher is better)")
        if design_era:
            lines.append(f"Design era: {design_era}")
        if tech_stack:
            lines.append(f"Built with: {', '.join(tech_stack[:5])}")
        lines.append(f"Contact form on site: {'yes' if has_contact_form else 'no'}")
        if page_title:
            lines.append(f"Page title: {page_title}")
        if meta_description:
            lines.append(f"Meta description: {meta_description[:200]}")

        if problems:
            lines.append("")
            lines.append("### Problems observed")
            lines.extend(f"- {p}" for p in problems[:12])
        else:
            lines.append("")
            lines.append("No significant problems were observed on this site.")

        if content_excerpt:
            lines.append("")
            lines.append("### Homepage text excerpt")
            lines.append(content_excerpt[:1200])

    lines.append("")
    lines.append("## Context")
    lines.append(
        f"A typical strong website in the {niche_label.lower()} category has: "
        f"{niche_expectation}."
    )
    lines.append("")
    lines.append(
        "Analyze this business as a prospect for website design work and record your "
        "analysis with the record_analysis tool. Use only the observations above."
    )
    return "\n".join(lines)


CATEGORIZE_TOOL: dict[str, Any] = {
    "name": "categorize",
    "description": "Assign each business to the best-fitting niche from the provided list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "niche_key": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["index", "niche_key", "confidence"],
                },
            }
        },
        "required": ["assignments"],
    },
}


def build_categorize_prompt(businesses: list[dict[str, Any]], niche_keys: list[str]) -> str:
    """Batch-classify ambiguous businesses into niches.

    Batched because one API call for 25 businesses costs a fraction of 25 calls
    and the task has no cross-contamination risk — each line is independent.
    """
    lines = [
        "Assign each numbered business below to exactly one niche key.",
        "",
        "Available niche keys:",
        ", ".join(niche_keys),
        "",
        "If none fits well, use the closest and set confidence below 0.5.",
        "",
        "Businesses:",
    ]
    for index, business in enumerate(businesses):
        categories = ", ".join(business.get("categories", [])[:4])
        lines.append(
            f"{index}. {business.get('name', '?')}"
            + (f" — listed as: {categories}" if categories else "")
            + (f" — {business['description'][:120]}" if business.get("description") else "")
        )
    return "\n".join(lines)
