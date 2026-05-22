"""AI-powered readability analysis and enhancement for the SEO Agent.

Analyses on-page readability using basic text metrics (sentence length,
passive voice, Flesch reading ease) and provides AI-driven paragraph
rewrites for improved clarity.

Uses httpx to call OpenAI API directly (no openai SDK dependency).
"""

import json
import logging
import re
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("seo.readability")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
TIMEOUT = 30.0

SYSTEM_PROMPT = (
    "You are a readability specialist for Pet Hub Online (pethubonline.com), "
    "a UK-based pet supplies affiliate website. You improve content clarity "
    "and readability while preserving meaning and SEO value. You write in "
    "British English and keep a friendly, informative tone."
)


async def _call_openai(
    messages: list[dict],
    model: str = "gpt-4o",
    temperature: float = 0.4,
    max_tokens: int = 600,
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


def _strip_html(html: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using punctuation boundaries."""
    # Split on sentence-ending punctuation followed by a space or end of string
    raw = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in raw if s.strip() and len(s.strip()) > 2]


def _count_words(text: str) -> int:
    """Count words in plain text."""
    return len(text.split())


def _count_syllables(word: str) -> int:
    """Estimate syllable count using a simple vowel-group heuristic."""
    word = word.lower().strip()
    if not word:
        return 1

    # Remove trailing silent 'e'
    if word.endswith("e") and len(word) > 2:
        word = word[:-1]

    # Count vowel groups
    vowel_groups = re.findall(r"[aeiouy]+", word)
    count = len(vowel_groups)

    # Every word has at least one syllable
    return max(count, 1)


def _count_syllables_total(text: str) -> int:
    """Count total syllables in a text."""
    words = re.findall(r"[a-zA-Z]+", text)
    return sum(_count_syllables(w) for w in words)


def _detect_passive_voice(sentences: list[str]) -> list[str]:
    """Detect sentences that likely use passive voice.

    Uses a simple heuristic: form of 'to be' followed by a past participle
    (word ending in -ed, -en, -t with specific patterns).
    """
    be_forms = r"\b(?:is|are|was|were|been|being|be)\b"
    # Past participle pattern: -ed, -en, or common irregular endings
    past_participle = r"\b\w+(?:ed|en|wn|nt|ught|ade|orn|ung|oken)\b"
    pattern = re.compile(
        rf"{be_forms}\s+(?:\w+\s+)?{past_participle}",
        re.IGNORECASE,
    )

    passive_sentences = []
    for sentence in sentences:
        if pattern.search(sentence):
            passive_sentences.append(sentence)

    return passive_sentences


def _count_headings(html: str) -> dict:
    """Count heading tags in HTML content."""
    counts = {}
    for level in range(1, 7):
        tag = f"h{level}"
        matches = re.findall(rf"<{tag}[\s>]", html, re.I)
        if matches:
            counts[tag] = len(matches)
    return counts


async def enhance_readability(title: str, content_html: str) -> dict:
    """Analyse and suggest readability improvements for a page.

    Args:
        title: Page title.
        content_html: Raw HTML content of the page.

    Returns:
        Dict with:
        - readability_score: int (0-100, higher = more readable)
        - avg_sentence_length: float (words per sentence)
        - issues: list of identified readability problems
        - suggestions: list of actionable improvement suggestions
    """
    fallback = {
        "readability_score": 0,
        "avg_sentence_length": 0.0,
        "issues": [],
        "suggestions": [],
    }

    if not content_html:
        logger.warning("No content HTML provided for readability analysis")
        return fallback

    # Strip HTML to get plain text
    plain_text = _strip_html(content_html)
    if not plain_text or len(plain_text) < 20:
        logger.warning("Content too short for meaningful readability analysis")
        return fallback

    # Split into sentences
    sentences = _split_sentences(plain_text)
    if not sentences:
        return fallback

    # Calculate metrics
    total_words = _count_words(plain_text)
    total_sentences = len(sentences)
    total_syllables = _count_syllables_total(plain_text)

    avg_sentence_length = total_words / total_sentences if total_sentences > 0 else 0.0

    # Flesch reading ease: 206.835 - 1.015*(words/sentences) - 84.6*(syllables/words)
    avg_syllables_per_word = total_syllables / total_words if total_words > 0 else 0.0
    flesch_score = (
        206.835
        - 1.015 * avg_sentence_length
        - 84.6 * avg_syllables_per_word
    )
    # Clamp to 0-100
    flesch_score = max(0.0, min(100.0, flesch_score))

    # Detect passive voice
    passive_sentences = _detect_passive_voice(sentences)
    passive_ratio = len(passive_sentences) / total_sentences if total_sentences > 0 else 0.0

    # Heading distribution
    headings = _count_headings(content_html)

    # Build issues list
    issues: list[str] = []

    if avg_sentence_length > 25:
        issues.append(
            f"Average sentence length is {avg_sentence_length:.1f} words "
            "(aim for 15-20 words for optimal readability)"
        )
    elif avg_sentence_length < 8:
        issues.append(
            f"Average sentence length is {avg_sentence_length:.1f} words "
            "(sentences may be too choppy; vary sentence length)"
        )

    if passive_ratio > 0.3:
        issues.append(
            f"{len(passive_sentences)} of {total_sentences} sentences "
            f"({passive_ratio:.0%}) appear to use passive voice (aim for under 15%)"
        )

    if flesch_score < 40:
        issues.append(
            f"Flesch reading ease score is {flesch_score:.0f}/100 "
            "(content is difficult to read; aim for 60-70 for general audiences)"
        )

    if not headings:
        issues.append("No headings found; add H2/H3 headings to break up content")
    elif "h2" not in headings:
        issues.append("No H2 headings found; use H2 tags for main content sections")
    else:
        words_per_heading = total_words / sum(headings.values())
        if words_per_heading > 300:
            issues.append(
                f"Content has roughly {words_per_heading:.0f} words per heading "
                "(aim for 150-250 words between headings)"
            )

    if total_words < 300:
        issues.append(
            f"Content is only {total_words} words (aim for 800+ words "
            "for comprehensive coverage)"
        )

    # Build suggestions
    suggestions: list[str] = []

    if avg_sentence_length > 20:
        suggestions.append(
            "Break long sentences into shorter ones. Look for sentences with "
            "'and', 'but', 'which' that could be split."
        )

    if passive_ratio > 0.15:
        suggestions.append(
            "Rewrite passive voice sentences using active voice "
            "(e.g., 'The food was eaten by the dog' -> 'The dog ate the food')."
        )

    if flesch_score < 60:
        suggestions.append(
            "Simplify vocabulary - replace complex words with everyday alternatives "
            "(e.g., 'utilise' -> 'use', 'subsequently' -> 'then')."
        )

    if not headings.get("h2"):
        suggestions.append(
            "Add H2 headings to structure the content into clear sections "
            "that readers can scan."
        )

    if total_words > 500 and not headings.get("h3"):
        suggestions.append(
            "Consider adding H3 subheadings within sections for longer content "
            "to improve scannability."
        )

    # Construct the readability score as a composite
    # Base: Flesch score (0-100), then penalise for issues
    readability_score = int(flesch_score)
    # Penalise for very long sentences
    if avg_sentence_length > 25:
        readability_score = max(0, readability_score - 15)
    elif avg_sentence_length > 20:
        readability_score = max(0, readability_score - 5)
    # Penalise for heavy passive voice
    if passive_ratio > 0.3:
        readability_score = max(0, readability_score - 10)
    elif passive_ratio > 0.15:
        readability_score = max(0, readability_score - 5)
    # Penalise for missing headings
    if not headings:
        readability_score = max(0, readability_score - 10)

    readability_score = max(0, min(100, readability_score))

    return {
        "readability_score": readability_score,
        "avg_sentence_length": round(avg_sentence_length, 1),
        "issues": issues,
        "suggestions": suggestions,
    }


async def ai_rewrite_paragraph(
    paragraph: str,
    target_reading_level: str = "general",
) -> Optional[str]:
    """Use AI to rewrite a paragraph for better readability.

    Args:
        paragraph: The paragraph text to rewrite.
        target_reading_level: Reading level target. One of "general" (default),
            "simple" (for accessibility), or "professional".

    Returns:
        Rewritten paragraph text, or None on failure.
    """
    if not paragraph or len(paragraph.strip()) < 10:
        logger.warning("Paragraph too short to rewrite")
        return None

    level_guidance = {
        "general": (
            "Write for a general UK audience (Flesch score 60-70). "
            "Use everyday language and keep sentences to 15-20 words on average."
        ),
        "simple": (
            "Write for maximum accessibility (Flesch score 80+). "
            "Use simple words, short sentences (under 15 words), and avoid jargon."
        ),
        "professional": (
            "Write for an informed audience (Flesch score 50-60). "
            "Maintain professional tone but keep sentences clear and well-structured."
        ),
    }

    guidance = level_guidance.get(target_reading_level, level_guidance["general"])

    user_prompt = (
        f"Rewrite the following paragraph to improve its readability. {guidance}\n\n"
        "Rules:\n"
        "- Keep the same meaning and key information\n"
        "- Use active voice wherever possible\n"
        "- Maintain any SEO-relevant keywords\n"
        "- Use British English spelling\n"
        "- Do not add new information or change facts\n\n"
        f"Original paragraph:\n{paragraph}\n\n"
        "Return ONLY the rewritten paragraph, no explanation or formatting."
    )

    result = await _call_openai(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        model="gpt-4o",
        temperature=0.4,
        max_tokens=500,
    )

    if result:
        # Clean up any surrounding quotes the AI might add
        result = result.strip('"\'')

    return result
