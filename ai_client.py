"""OpenAI GPT integration for the SEO Agent.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
Provides AI-powered meta description generation, anchor text suggestions,
and content improvement recommendations.
"""

import json
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("seo.ai")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 15.0

SYSTEM_PROMPT = (
    "You are an SEO specialist for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You optimise content for search engines "
    "while keeping it natural and reader-friendly. You follow Google's E-E-A-T guidelines."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o-mini",
    temperature: float = 0.5,
    max_tokens: int = 400,
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
        logger.error("OpenAI API HTTP %d: %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        logger.error("OpenAI API unexpected error: %s", exc)
        return None


async def ai_generate_meta_description(
    title: str,
    content_snippet: str,
    max_chars: int = 155,
) -> Optional[str]:
    """Generate an SEO-optimised meta description for a page.

    Args:
        title: Page title.
        content_snippet: First ~200 chars of page content.
        max_chars: Maximum character length for the description (default 155).

    Returns:
        Meta description string, or None if the API call fails.
    """
    user_prompt = (
        f"Page title: {title}\n"
        f"Content snippet: {content_snippet}\n\n"
        f"Write an SEO-optimised meta description in under {max_chars} characters. "
        "Include the primary keyword from the title naturally. "
        "Make it compelling to encourage clicks from search results. "
        "Use active voice and include a subtle call-to-action.\n\n"
        "Return ONLY the meta description text, nothing else."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=100,
    )

    if result and len(result) > max_chars:
        # Truncate at the last complete word within the limit
        truncated = result[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.7:
            result = truncated[:last_space].rstrip(".,;:!?") + "..."
        else:
            result = truncated.rstrip(".,;:!?") + "..."
        logger.info("Truncated meta description from %d to %d chars", len(result), max_chars)

    return result


async def ai_suggest_anchor_text(
    source_title: str,
    source_snippet: str,
    target_title: str,
) -> Optional[str]:
    """Suggest natural anchor text for an internal link.

    Args:
        source_title: Title of the page where the link will be placed.
        source_snippet: Content snippet from the source page.
        target_title: Title of the page being linked to.

    Returns:
        Anchor text string (2-4 words), or None if the API call fails.
    """
    user_prompt = (
        f"Source page title: {source_title}\n"
        f"Source page content: {source_snippet}\n"
        f"Target page title: {target_title}\n\n"
        "Suggest natural anchor text (2-4 words) for an internal link from the source page "
        "to the target page. The anchor text should:\n"
        "- Feel natural within the source page content\n"
        "- Include a relevant keyword from the target page\n"
        "- Not be generic (avoid 'click here', 'read more')\n\n"
        "Return ONLY the anchor text, nothing else."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=30,
    )

    if result:
        # Clean up any quotes or extra punctuation
        result = result.strip('"\'.,;:!?')

    return result


async def ai_content_improvement_suggestions(
    title: str,
    content_snippet: str,
    seo_score: int,
) -> Optional[list[str]]:
    """Suggest specific improvements for a page's SEO and content quality.

    Args:
        title: Page title.
        content_snippet: First ~500 chars of page content.
        seo_score: Current SEO score (0-100).

    Returns:
        List of 3-5 improvement suggestion strings, or None on failure.
    """
    user_prompt = (
        f"Page title: {title}\n"
        f"Current SEO score: {seo_score}/100\n"
        f"Content snippet: {content_snippet}\n\n"
        "Provide 3-5 specific, actionable improvements to boost this page's SEO. "
        "Consider: keyword placement, content structure, readability, internal linking "
        "opportunities, and schema markup.\n\n"
        "Return a JSON array of strings, each being one specific suggestion.\n"
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
        return None

    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return [str(s) for s in parsed]
        logger.warning("OpenAI returned non-list for suggestions: %s", type(parsed))
        return None
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse improvement suggestions JSON: %s", exc)
        return None
