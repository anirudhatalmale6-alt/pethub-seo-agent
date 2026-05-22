"""AI-powered competitor gap analysis for the SEO Agent.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
Analyzes top-ranking pages for target keywords and compares them against
the site's existing content to find gaps and opportunities in the UK pet
supplies niche.
"""

import json
import logging
import re
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("seo.competitor")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 30.0

SYSTEM_PROMPT = (
    "You are a competitive SEO analyst for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You specialise in identifying "
    "content gaps, keyword opportunities, and competitive positioning in the "
    "UK pet supplies market. You are data-driven and precise."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o",
    temperature: float = 0.4,
    max_tokens: int = 1200,
) -> Optional[str]:
    """Low-level helper to call the OpenAI chat completions endpoint."""
    headers = {
        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(OPENAI_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except httpx.TimeoutException:
        logger.error("OpenAI API timeout after %.0fs", TIMEOUT)
        return None
    except httpx.HTTPStatusError as exc:
        logger.error(
            "OpenAI API HTTP %d: %s",
            exc.response.status_code,
            exc.response.text[:300],
        )
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None


def _clean_json(raw: str) -> str:
    """Strip markdown code fences from an AI response if present."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    return cleaned


def _extract_keywords_from_titles(pages: list[dict]) -> list[str]:
    """Extract meaningful keywords from page titles."""
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "it", "this", "that", "are", "was",
        "be", "has", "had", "have", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "your", "our", "their",
        "its", "my", "best", "top", "guide", "how", "what", "why", "when",
        "uk", "online", "buy", "shop", "pet", "hub",
    }
    keywords: list[str] = []
    seen: set[str] = set()

    for page in pages:
        title = page.get("title", "")
        # Extract 2-3 word phrases from titles
        words = re.findall(r"[a-zA-Z]+", title.lower())
        meaningful = [w for w in words if w not in stopwords and len(w) > 2]

        # Add individual keywords
        for word in meaningful:
            if word not in seen:
                seen.add(word)
                keywords.append(word)

        # Add two-word combinations
        for i in range(len(meaningful) - 1):
            phrase = f"{meaningful[i]} {meaningful[i + 1]}"
            if phrase not in seen:
                seen.add(phrase)
                keywords.append(phrase)

    return keywords[:30]  # Cap at 30 keywords


async def analyze_competitor_gaps(
    existing_pages: list[dict],
    target_keywords: list[str] = None,
) -> dict:
    """Analyze content gaps compared to typical competitor sites.

    Args:
        existing_pages: List of page dicts with keys: title, url, word_count, seo_score.
        target_keywords: Optional list of keywords to focus analysis on.
            If not provided, keywords are extracted from page titles.

    Returns:
        Dict with:
        - gaps: list of gap dicts (keyword, opportunity, priority, suggested_title)
        - existing_coverage: summary of current coverage
        - overall_score: 0-100 competitiveness score
    """
    fallback = {
        "gaps": [],
        "existing_coverage": "Unable to analyse coverage.",
        "overall_score": 0,
    }

    if not existing_pages:
        logger.warning("No existing pages provided for competitor gap analysis")
        return fallback

    # Extract or use provided keywords
    if not target_keywords:
        target_keywords = _extract_keywords_from_titles(existing_pages)
        if not target_keywords:
            logger.warning("Could not extract keywords from page titles")
            return fallback

    # Build a summary of existing pages
    pages_summary = "\n".join(
        f"- {p.get('title', 'Untitled')} ({p.get('word_count', 0)} words, "
        f"SEO score: {p.get('seo_score', 0)}/100)"
        for p in existing_pages[:50]  # Limit to avoid token overflow
    )

    keywords_str = ", ".join(target_keywords[:20])

    user_prompt = (
        "Analyse the following existing pages on a UK pet supplies affiliate website "
        "and identify content gaps compared to what top-ranking competitor sites in "
        "the UK pet supplies niche typically cover.\n\n"
        f"Existing pages ({len(existing_pages)} total):\n{pages_summary}\n\n"
        f"Target keywords to focus on: {keywords_str}\n\n"
        "Consider what major UK pet supplies competitors (Pets at Home, Zooplus UK, "
        "Pet Planet, VetShop, Monster Pet Supplies) would typically cover.\n\n"
        "Return a JSON object with:\n"
        '- "gaps": array of objects, each with:\n'
        '  - "keyword": the keyword or topic gap\n'
        '  - "opportunity": brief explanation of why this is valuable\n'
        '  - "priority": "high", "medium", or "low"\n'
        '  - "suggested_title": a suggested page/article title\n'
        '- "existing_coverage": a 2-3 sentence summary of what the site covers well\n'
        '- "overall_score": integer 0-100 rating of competitive coverage\n\n'
        "Identify 5-10 gaps. Return ONLY the JSON object, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.4,
        max_tokens=1500,
    )

    if result is None:
        logger.error("Failed to get competitor gap analysis from OpenAI")
        return fallback

    try:
        parsed = json.loads(_clean_json(result))
        if not isinstance(parsed, dict):
            logger.warning("OpenAI returned non-dict for gap analysis")
            return fallback

        gaps = []
        for gap in parsed.get("gaps", []):
            if isinstance(gap, dict):
                gaps.append({
                    "keyword": str(gap.get("keyword", "")),
                    "opportunity": str(gap.get("opportunity", "")),
                    "priority": str(gap.get("priority", "medium")).lower(),
                    "suggested_title": str(gap.get("suggested_title", "")),
                })

        return {
            "gaps": gaps,
            "existing_coverage": str(parsed.get("existing_coverage", "No analysis available.")),
            "overall_score": int(parsed.get("overall_score", 0)),
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed to parse competitor gap analysis JSON: %s", exc)
        return fallback


async def analyze_page_vs_competitors(
    page_title: str,
    page_content_snippet: str,
    target_keyword: str,
) -> dict:
    """Assess how a page compares to what top-ranking pages typically cover.

    Args:
        page_title: Title of the page being analysed.
        page_content_snippet: First ~500 chars of page content.
        target_keyword: The primary keyword the page targets.

    Returns:
        Dict with:
        - score: 0-100 competitive comparison score
        - missing_topics: list of topics competitors cover that this page doesn't
        - strengths: list of things this page does well
        - suggestions: list of actionable improvements
    """
    fallback = {
        "score": 0,
        "missing_topics": [],
        "strengths": [],
        "suggestions": [],
    }

    if not page_title or not target_keyword:
        logger.warning("Missing page_title or target_keyword for competitor comparison")
        return fallback

    user_prompt = (
        f"Target keyword: {target_keyword}\n"
        f"Page title: {page_title}\n"
        f"Content snippet: {page_content_snippet[:500] if page_content_snippet else '(no content)'}\n\n"
        "Based on your knowledge of what top-ranking pages for this keyword in the "
        "UK pet supplies market typically cover, assess this page.\n\n"
        "Consider what the top 5-10 search results for this keyword would typically include: "
        "topics covered, content depth, product comparisons, buying guides, FAQs, etc.\n\n"
        "Return a JSON object with:\n"
        '- "score": integer 0-100 (how well this page competes)\n'
        '- "missing_topics": array of topics that top-ranking pages cover but this one does not\n'
        '- "strengths": array of things this page does well compared to competitors\n'
        '- "suggestions": array of specific improvements to make the page more competitive\n\n'
        "Keep each array to 3-5 items. Return ONLY the JSON object, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.4,
        max_tokens=800,
    )

    if result is None:
        logger.error("Failed to get page vs competitors analysis from OpenAI")
        return fallback

    try:
        parsed = json.loads(_clean_json(result))
        if not isinstance(parsed, dict):
            logger.warning("OpenAI returned non-dict for page comparison")
            return fallback

        return {
            "score": int(parsed.get("score", 0)),
            "missing_topics": [str(t) for t in parsed.get("missing_topics", [])],
            "strengths": [str(s) for s in parsed.get("strengths", [])],
            "suggestions": [str(s) for s in parsed.get("suggestions", [])],
        }
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.error("Failed to parse page comparison JSON: %s", exc)
        return fallback


async def suggest_content_improvements(
    title: str,
    content_snippet: str,
) -> list[str]:
    """Analyse content and return 3-5 specific readability and SEO improvements.

    Focuses on sentence structure, keyword placement, heading usage, and
    content depth.

    Args:
        title: Page title.
        content_snippet: First ~500 chars of page content.

    Returns:
        List of 3-5 improvement suggestion strings. Returns empty list on failure.
    """
    if not title:
        logger.warning("Missing title for content improvement suggestions")
        return []

    user_prompt = (
        f"Page title: {title}\n"
        f"Content snippet: {content_snippet[:500] if content_snippet else '(no content)'}\n\n"
        "Provide 3-5 specific, actionable improvements for this UK pet supplies page. "
        "Focus on:\n"
        "1. Sentence structure - are sentences too long, too complex, or monotonous?\n"
        "2. Keyword placement - is the primary keyword used naturally and strategically?\n"
        "3. Heading usage - does the content structure use headings effectively?\n"
        "4. Content depth - is the topic covered thoroughly enough?\n"
        "5. Readability - is the content accessible to a general audience?\n\n"
        "Return a JSON array of 3-5 strings, each being one specific actionable suggestion.\n"
        "Return ONLY the JSON array, no markdown formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.5,
        max_tokens=500,
    )

    if result is None:
        logger.error("Failed to get content improvement suggestions from OpenAI")
        return []

    try:
        parsed = json.loads(_clean_json(result))
        if isinstance(parsed, list):
            return [str(s) for s in parsed[:5]]
        logger.warning(
            "OpenAI returned non-list for improvement suggestions: %s",
            type(parsed),
        )
        return []
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Failed to parse improvement suggestions JSON: %s", exc)
        return []
