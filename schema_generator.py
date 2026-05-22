import re
import json
import logging
import base64
from html.parser import HTMLParser
import httpx
from config import settings

logger = logging.getLogger("seo-agent.schema")

WP_AUTH = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
WP_HEADERS = {"Authorization": WP_AUTH, "Content-Type": "application/json"}
WP = f"{settings.WP_URL}/wp-json/wp/v2"

QUESTION_WORDS = ("what", "how", "why", "when", "where", "which", "can", "do", "does", "is", "are", "will", "should")

PRODUCT_TERMS = (
    "buy", "price", "shop", "order", "add to cart", "checkout", "product",
    "review", "rating", "best", "top", "recommended", "deal", "discount",
    "affordable", "cheap", "premium", "value", "quality",
)

PRICE_PATTERN = re.compile(r"£\s*(\d+(?:\.\d{2})?)")
STAR_PATTERN = re.compile(r"(\d(?:\.\d)?)\s*(?:out of\s*5|/\s*5|stars?|★)", re.IGNORECASE)
RATING_PATTERN = re.compile(r"(?:rating|rated|score)[:\s]*(\d(?:\.\d)?)\s*/?\s*5", re.IGNORECASE)


class FAQExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.faqs = []
        self._tag = None
        self._text = ""
        self._last_question = None
        self._collecting_answer = False
        self._answer_text = ""

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3"):
            if self._collecting_answer and self._last_question and self._answer_text.strip():
                self.faqs.append({"question": self._last_question, "answer": self._answer_text.strip()})
                self._collecting_answer = False
                self._answer_text = ""
                self._last_question = None
            self._tag = tag
            self._text = ""
        elif tag == "p" and self._last_question:
            self._collecting_answer = True

    def handle_data(self, data):
        if self._tag:
            self._text += data
        elif self._collecting_answer:
            self._answer_text += data

    def handle_endtag(self, tag):
        if tag == self._tag:
            heading_text = self._text.strip()
            is_question = heading_text.endswith("?") or heading_text.lower().startswith(QUESTION_WORDS)
            if is_question:
                if self._last_question and self._answer_text.strip():
                    self.faqs.append({"question": self._last_question, "answer": self._answer_text.strip()})
                self._last_question = heading_text
                self._answer_text = ""
                self._collecting_answer = False
            self._tag = None
        elif tag == "p" and self._collecting_answer:
            self._answer_text += " "

    def close(self):
        super().close()
        if self._last_question and self._answer_text.strip():
            self.faqs.append({"question": self._last_question, "answer": self._answer_text.strip()})


def extract_faqs(content_html: str) -> list:
    parser = FAQExtractor()
    parser.feed(content_html)
    parser.close()
    return parser.faqs


def generate_faq_schema(faqs: list, page_url: str) -> dict | None:
    if not faqs:
        return None
    entities = []
    for faq in faqs:
        answer_clean = re.sub(r"<[^>]+>", "", faq["answer"]).strip()
        if len(answer_clean) < 10:
            continue
        entities.append({
            "@type": "Question",
            "name": faq["question"],
            "acceptedAnswer": {
                "@type": "Answer",
                "text": answer_clean,
            }
        })
    if not entities:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": entities,
    }


def detect_product_page(title: str, content_html: str) -> bool:
    text = re.sub(r"<[^>]+>", " ", content_html).lower()
    has_price = bool(PRICE_PATTERN.search(content_html))
    term_count = sum(1 for t in PRODUCT_TERMS if t in text)
    has_affiliate = bool(re.search(r'rel=".*?nofollow.*?"', content_html)) or "affiliate" in text
    return has_price or (term_count >= 3 and has_affiliate)


def extract_price(content_html: str) -> str | None:
    match = PRICE_PATTERN.search(content_html)
    return match.group(1) if match else None


def extract_first_paragraph(content_html: str) -> str:
    match = re.search(r"<p[^>]*>(.*?)</p>", content_html, re.DOTALL)
    if match:
        return re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return ""


def generate_product_schema(page: dict, content_html: str) -> dict | None:
    title = re.sub(r"<[^>]+>", "", page.get("title", "")).strip()
    if not detect_product_page(title, content_html):
        return None

    price = extract_price(content_html)
    if not price:
        return None

    description = page.get("meta_description", "") or extract_first_paragraph(content_html)
    if len(description) > 300:
        description = description[:297] + "..."

    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": title,
        "description": description,
        "url": page.get("url", ""),
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "GBP",
            "availability": "https://schema.org/InStock",
            "url": page.get("url", ""),
        }
    }


def detect_review_content(content_html: str) -> dict | None:
    text = re.sub(r"<[^>]+>", " ", content_html)
    match = STAR_PATTERN.search(text) or RATING_PATTERN.search(text)
    if match:
        return {"rating": match.group(1)}
    return None


def generate_review_schema(page: dict, content_html: str) -> dict | None:
    review_info = detect_review_content(content_html)
    if not review_info:
        return None

    title = re.sub(r"<[^>]+>", "", page.get("title", "")).strip()
    description = page.get("meta_description", "") or extract_first_paragraph(content_html)

    return {
        "@context": "https://schema.org",
        "@type": "Review",
        "name": title,
        "reviewBody": description[:300] if description else title,
        "author": {
            "@type": "Organization",
            "name": "Pet Hub Online",
        },
        "itemReviewed": {
            "@type": "Thing",
            "name": title,
        },
        "reviewRating": {
            "@type": "Rating",
            "ratingValue": review_info["rating"],
            "bestRating": "5",
        }
    }


def generate_schemas_for_page(page: dict, content_html: str) -> list:
    schemas = []
    title = re.sub(r"<[^>]+>", "", page.get("title", "")).strip()

    faqs = extract_faqs(content_html)
    faq_schema = generate_faq_schema(faqs, page.get("url", ""))
    if faq_schema:
        schemas.append({"type": "FAQPage", "schema": faq_schema})

    product_schema = generate_product_schema(page, content_html)
    if product_schema:
        schemas.append({"type": "Product", "schema": product_schema})

    review_schema = generate_review_schema(page, content_html)
    if review_schema:
        schemas.append({"type": "Review", "schema": review_schema})

    return schemas


async def inject_schema(page_id: int, schema_json: dict, content_type: str = "posts") -> bool:
    schema_str = json.dumps(schema_json)
    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in [content_type, "pages" if content_type == "posts" else "posts"]:
            resp = await client.post(
                f"{WP}/{endpoint}/{page_id}",
                headers=WP_HEADERS,
                json={"meta": {"rank_math_schema": schema_str}},
            )
            if resp.status_code == 200:
                logger.info(f"Injected {schema_json.get('@type', 'unknown')} schema for page {page_id}")
                return True
    logger.warning(f"Failed to inject schema for page {page_id}")
    return False


async def generate_and_inject_schemas(audit_results: list) -> dict:
    total_generated = 0
    total_injected = 0
    pages_processed = 0
    details = []

    async with httpx.AsyncClient(timeout=15) as client:
        for page in audit_results:
            page_id = page["page_id"]
            title = page["title"]
            has_schema = page.get("meta", {}).get("has_schema", False)

            if has_schema:
                details.append({
                    "page_id": page_id,
                    "title": title,
                    "status": "skipped",
                    "reason": "already has schema",
                })
                continue

            content_html = ""
            for endpoint in ["pages", "posts"]:
                resp = await client.get(
                    f"{WP}/{endpoint}/{page_id}",
                    headers=WP_HEADERS,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content_html = data.get("content", {}).get("rendered", "")
                    break

            if not content_html:
                continue

            schemas = generate_schemas_for_page(page, content_html)
            pages_processed += 1

            if not schemas:
                details.append({
                    "page_id": page_id,
                    "title": title,
                    "status": "no_schema_applicable",
                })
                continue

            total_generated += len(schemas)

            for schema_entry in schemas:
                success = await inject_schema(page_id, schema_entry["schema"])
                if success:
                    total_injected += 1
                details.append({
                    "page_id": page_id,
                    "title": title,
                    "status": "injected" if success else "failed",
                    "schema_type": schema_entry["type"],
                })

    return {
        "pages_processed": pages_processed,
        "schemas_generated": total_generated,
        "schemas_injected": total_injected,
        "details": details,
    }
