import re
import logging
import base64
from html.parser import HTMLParser
import httpx
from config import settings

logger = logging.getLogger("seo-agent.linker")

WP_AUTH = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
WP_HEADERS = {"Authorization": WP_AUTH, "Content-Type": "application/json"}
WP = f"{settings.WP_URL}/wp-json/wp/v2"

MAX_LINKS_PER_PAGE = 3

PET_CATEGORIES = {
    "cat": ["cat", "cats", "kitten", "kittens", "feline"],
    "dog": ["dog", "dogs", "puppy", "puppies", "canine"],
    "fish": ["fish", "aquarium", "aquatic", "tank", "tropical"],
    "bird": ["bird", "birds", "parrot", "avian", "budgie"],
    "rabbit": ["rabbit", "rabbits", "bunny", "bunnies"],
    "hamster": ["hamster", "hamsters", "gerbil", "rodent"],
    "reptile": ["reptile", "reptiles", "snake", "lizard", "gecko", "turtle"],
    "general": ["pet", "pets", "animal", "animals"],
}

TOPIC_KEYWORDS = [
    "food", "feeding", "diet", "nutrition",
    "health", "vet", "illness", "disease", "symptom",
    "training", "behavior", "behaviour",
    "grooming", "care", "cleaning",
    "toy", "toys", "play", "exercise",
    "bed", "bedding", "cage", "crate", "kennel",
    "collar", "leash", "harness",
    "breed", "breeds",
    "insurance", "cost", "price", "budget",
    "adoption", "rescue", "shelter",
    "review", "best", "top", "guide", "buying",
]


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self.hrefs.add(href.rstrip("/").lower())


class HeadingExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings = []
        self._tag = None
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3"):
            self._tag = tag
            self._text = ""

    def handle_data(self, data):
        if self._tag:
            self._text += data

    def handle_endtag(self, tag):
        if tag == self._tag:
            self.headings.append(self._text.strip().lower())
            self._tag = None


def extract_key_phrases(title: str, content_html: str) -> set:
    phrases = set()
    title_words = re.sub(r"[^\w\s]", "", title.lower()).split()
    for w in title_words:
        if len(w) > 3:
            phrases.add(w)

    parser = HeadingExtractor()
    parser.feed(content_html)
    for heading in parser.headings:
        words = re.sub(r"[^\w\s]", "", heading).split()
        for w in words:
            if len(w) > 3:
                phrases.add(w)

    text = re.sub(r"<[^>]+>", " ", content_html)
    first_para = text[:500].lower()
    for w in re.sub(r"[^\w\s]", "", first_para).split():
        if len(w) > 4:
            phrases.add(w)

    stop_words = {
        "this", "that", "with", "from", "your", "they", "them", "their",
        "have", "been", "were", "will", "would", "could", "should", "about",
        "more", "most", "some", "also", "than", "then", "into", "over",
        "these", "those", "which", "there", "where", "when", "what",
        "very", "just", "only", "here", "other", "many", "much",
        "such", "like", "make", "made", "well", "back", "even", "each",
    }
    phrases -= stop_words
    return phrases


def get_pet_category(text: str) -> set:
    text_lower = text.lower()
    categories = set()
    for cat, terms in PET_CATEGORIES.items():
        if any(t in text_lower for t in terms):
            categories.add(cat)
    return categories


def get_primary_species(title: str, slug: str) -> str | None:
    """Determine the primary species from title/slug only (not full content)."""
    text = (title + " " + slug.replace("-", " ")).lower()
    for species in ["cat", "dog", "fish", "bird", "rabbit", "hamster", "reptile"]:
        terms = PET_CATEGORIES[species]
        if any(t in text for t in terms):
            return species
    return None


def get_topics(text: str) -> set:
    text_lower = text.lower()
    return {kw for kw in TOPIC_KEYWORDS if kw in text_lower}


def compute_relevance(page_a: dict, page_b: dict) -> float:
    if page_a["url"].rstrip("/") == page_b["url"].rstrip("/"):
        return 0.0

    sp_a = page_a.get("primary_species")
    sp_b = page_b.get("primary_species")
    if sp_a and sp_b and sp_a != sp_b:
        return 0.0

    shared_phrases = page_a["key_phrases"] & page_b["key_phrases"]
    phrase_score = min(len(shared_phrases) / max(len(page_a["key_phrases"]), 1), 1.0)

    shared_categories = page_a["categories"] & page_b["categories"]
    cat_score = 1.0 if shared_categories else 0.0

    shared_topics = page_a["topics"] & page_b["topics"]
    topic_score = min(len(shared_topics) / 3, 1.0)

    return (phrase_score * 0.4) + (cat_score * 0.3) + (topic_score * 0.3)


async def build_content_map() -> list:
    content_map = []
    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in ["pages", "posts"]:
            page_num = 1
            while True:
                resp = await client.get(
                    f"{WP}/{endpoint}",
                    headers=WP_HEADERS,
                    params={"per_page": 50, "page": page_num, "status": "publish"},
                )
                if resp.status_code != 200:
                    break
                batch = resp.json()
                if not batch:
                    break
                for item in batch:
                    title = re.sub(r"<[^>]+>", "", item.get("title", {}).get("rendered", "")).strip()
                    content = item.get("content", {}).get("rendered", "")
                    url = item.get("link", "")
                    slug = item.get("slug", "")

                    link_parser = LinkExtractor()
                    link_parser.feed(content)

                    key_phrases = extract_key_phrases(title, content)
                    full_text = title + " " + re.sub(r"<[^>]+>", " ", content)

                    content_map.append({
                        "id": item["id"],
                        "title": title,
                        "slug": slug,
                        "url": url,
                        "type": endpoint.rstrip("s"),
                        "key_phrases": key_phrases,
                        "categories": get_pet_category(full_text),
                        "primary_species": get_primary_species(title, slug),
                        "topics": get_topics(full_text),
                        "existing_links": link_parser.hrefs,
                        "content_html": content,
                    })
                if len(batch) < 50:
                    break
                page_num += 1
    return content_map


def find_link_opportunities(content_map: list) -> list:
    suggestions = []

    for page in content_map:
        page_suggestions = []
        existing = page["existing_links"]

        candidates = []
        for other in content_map:
            if other["id"] == page["id"]:
                continue
            normalized_url = other["url"].rstrip("/").lower()
            if normalized_url in existing:
                continue
            if other["slug"] and any(other["slug"] in link for link in existing):
                continue
            score = compute_relevance(page, other)
            if score > 0.5:
                candidates.append({"page": other, "score": score})

        candidates.sort(key=lambda c: c["score"], reverse=True)

        for candidate in candidates[:MAX_LINKS_PER_PAGE]:
            target = candidate["page"]
            anchor_text = find_anchor_opportunity(page["content_html"], target)
            if anchor_text:
                page_suggestions.append({
                    "source_id": page["id"],
                    "source_title": page["title"],
                    "source_url": page["url"],
                    "target_id": target["id"],
                    "target_title": target["title"],
                    "target_url": target["url"],
                    "anchor_text": anchor_text,
                    "relevance_score": round(candidate["score"], 2),
                })

        suggestions.extend(page_suggestions)

    return suggestions


BAD_ANCHORS = {
    "about", "number", "needs", "every", "online", "posts", "page",
    "click", "here", "more", "read", "view", "visit", "check", "find",
    "best", "guide", "must", "haves", "owner", "owners", "blog",
    "policy", "terms", "contact", "home", "welcome", "essential",
}


def find_anchor_opportunity(content_html: str, target_page: dict) -> str | None:
    text = re.sub(r"<[^>]+>", " ", content_html).lower()
    title_words = re.sub(r"[^\w\s]", "", target_page["title"].lower()).split()
    significant_words = [w for w in title_words if len(w) > 3 and w not in BAD_ANCHORS]

    if not significant_words:
        return None

    for phrase_len in range(min(len(significant_words), 4), 1, -1):
        for i in range(len(significant_words) - phrase_len + 1):
            phrase = " ".join(significant_words[i:i + phrase_len])
            if phrase in text:
                already_linked = bool(re.search(
                    r'<a\b[^>]*>[^<]*' + re.escape(phrase) + r'[^<]*</a>',
                    content_html, re.IGNORECASE
                ))
                if not already_linked:
                    return phrase.title()

    slug_phrase = target_page["slug"].replace("-", " ")
    slug_words_clean = [w for w in slug_phrase.split() if w not in BAD_ANCHORS and len(w) > 2]
    if len(slug_words_clean) >= 2:
        candidate = " ".join(slug_words_clean)
        if candidate in text:
            already_linked = bool(re.search(
                r'<a\b[^>]*>[^<]*' + re.escape(candidate) + r'[^<]*</a>',
                content_html, re.IGNORECASE
            ))
            if not already_linked:
                return candidate.title()

    return None


async def suggest_internal_links(audit_results: list) -> list:
    content_map = await build_content_map()
    suggestions = find_link_opportunities(content_map)
    logger.info(f"Generated {len(suggestions)} internal link suggestions")
    return suggestions


async def auto_add_internal_links(audit_results: list) -> dict:
    content_map = await build_content_map()
    suggestions = find_link_opportunities(content_map)

    added = 0
    failed = 0
    details = []

    links_per_page = {}
    for s in suggestions:
        sid = s["source_id"]
        if links_per_page.get(sid, 0) >= MAX_LINKS_PER_PAGE:
            continue
        links_per_page[sid] = links_per_page.get(sid, 0) + 1

        async with httpx.AsyncClient(timeout=15) as client:
            content_data = None
            used_endpoint = None
            for endpoint in ["pages", "posts"]:
                resp = await client.get(
                    f"{WP}/{endpoint}/{sid}?context=edit",
                    headers=WP_HEADERS,
                )
                if resp.status_code == 200:
                    content_data = resp.json()
                    used_endpoint = endpoint
                    break

            if not content_data:
                failed += 1
                continue

            raw_content = content_data.get("content", {}).get("raw", "")
            if not raw_content:
                failed += 1
                continue

            anchor = s["anchor_text"]
            target_url = s["target_url"]

            pattern = re.compile(
                r'(?<!</a>)(?<!href=["\'])(?<![>])'
                + re.escape(anchor),
                re.IGNORECASE
            )
            match = pattern.search(raw_content)
            if not match:
                continue

            linked_text = f'<a href="{target_url}">{match.group()}</a>'
            new_content = raw_content[:match.start()] + linked_text + raw_content[match.end():]

            resp = await client.post(
                f"{WP}/{used_endpoint}/{sid}",
                headers=WP_HEADERS,
                json={"content": new_content},
            )
            if resp.status_code == 200:
                added += 1
                details.append({
                    "source": s["source_title"],
                    "target": s["target_title"],
                    "anchor": anchor,
                    "status": "added",
                })
                logger.info(f"Added internal link: '{anchor}' -> {target_url} on page {sid}")
            else:
                failed += 1
                details.append({
                    "source": s["source_title"],
                    "target": s["target_title"],
                    "anchor": anchor,
                    "status": "failed",
                })

    return {
        "total_suggestions": len(suggestions),
        "links_added": added,
        "links_failed": failed,
        "details": details,
    }
