import re
import logging
import base64
from html.parser import HTMLParser
from datetime import datetime, timezone
import httpx
from config import settings

logger = logging.getLogger("seo-agent.auditor")

WP_AUTH = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
WP_HEADERS = {"Authorization": WP_AUTH}


class HTMLContentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []
        self.links = []
        self.headings = {"h1": [], "h2": [], "h3": [], "h4": []}
        self._current_tag = None
        self._current_data = ""
        self._current_attrs = {}

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "img":
            self.images.append({
                "src": attrs_dict.get("src", ""),
                "alt": attrs_dict.get("alt", ""),
                "title": attrs_dict.get("title", "")
            })
        elif tag == "a":
            href = attrs_dict.get("href", "")
            if href and not href.startswith("#") and not href.startswith("javascript:"):
                self.links.append({"href": href, "rel": attrs_dict.get("rel", "")})
        if tag in self.headings:
            self._current_tag = tag
            self._current_data = ""

    def handle_data(self, data):
        if self._current_tag:
            self._current_data += data

    def handle_endtag(self, tag):
        if tag == self._current_tag:
            self.headings[tag].append(self._current_data.strip())
            self._current_tag = None


async def fetch_page_head(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PetHubSEOBot/1.0)"})
            if resp.status_code == 200:
                head_match = re.search(r"<head[^>]*>(.*?)</head>", resp.text, re.DOTALL | re.IGNORECASE)
                return head_match.group(1) if head_match else resp.text[:15000]
    except Exception:
        pass
    return ""


async def analyze_meta(page: dict) -> dict:
    issues = []
    warnings = []
    passes = []
    title_raw = page.get("title", {}).get("rendered", "")
    title = re.sub(r"<[^>]+>", "", title_raw).strip()
    page_url = page.get("link", "")

    head_html = await fetch_page_head(page_url) if page_url else ""

    meta_title = ""
    title_matches = re.findall(r'<title>([^<]+)</title>', head_html)
    for t in title_matches:
        t = t.strip()
        if len(t) > len(meta_title):
            meta_title = t
    if not meta_title:
        meta_title = title

    meta_desc = ""
    desc_matches = re.findall(r'<meta\s+name="description"\s+content="([^"]*)"', head_html)
    for d in desc_matches:
        if len(d) > len(meta_desc):
            meta_desc = d

    og_title = ""
    m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', head_html)
    if m:
        og_title = m.group(1)

    og_desc = ""
    m = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', head_html)
    if m:
        og_desc = m.group(1)

    og_image = ""
    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', head_html)
    if m:
        og_image = m.group(1)

    canonical = ""
    m = re.search(r'<link\s+rel="canonical"\s+href="([^"]*)"', head_html)
    if not m:
        m = re.search(r'<link\s+href="([^"]*)"\s+rel="canonical"', head_html)
    if m:
        canonical = m.group(1)

    has_schema = "application/ld+json" in head_html

    robots = ""
    m = re.search(r'<meta\s+name="robots"\s+content="([^"]*)"', head_html)
    if m:
        robots = m.group(1).lower()

    viewport = ""
    m = re.search(r'<meta\s+name="viewport"\s+content="([^"]*)"', head_html)
    if m:
        viewport = m.group(1)

    if "noindex" in robots:
        issues.append("CRITICAL: Page has noindex robots tag - Google will NOT index this page")
    if "nofollow" in robots:
        warnings.append("Page has nofollow robots tag - link equity not being passed")

    if not meta_title:
        issues.append("Missing meta title")
    elif len(meta_title) < 30:
        warnings.append(f"Meta title too short ({len(meta_title)} chars, aim for 50-60)")
    elif len(meta_title) > 70:
        warnings.append(f"Meta title too long ({len(meta_title)} chars, aim for 50-60)")
    else:
        passes.append(f"Meta title OK ({len(meta_title)} chars)")

    if not meta_desc:
        issues.append("Missing meta description")
    elif len(meta_desc) < 100:
        warnings.append(f"Meta description too short ({len(meta_desc)} chars, aim for 150-160)")
    elif len(meta_desc) > 170:
        warnings.append(f"Meta description too long ({len(meta_desc)} chars, aim for 150-160)")
    else:
        passes.append(f"Meta description OK ({len(meta_desc)} chars)")

    if og_title:
        passes.append("Open Graph title present")
    else:
        warnings.append("Missing Open Graph title")

    if og_desc:
        passes.append("Open Graph description present")
    else:
        warnings.append("Missing Open Graph description")

    if og_image:
        passes.append("Open Graph image present")
    else:
        warnings.append("Missing Open Graph image")

    if canonical:
        passes.append("Canonical URL set")
    else:
        warnings.append("Missing canonical URL")

    if has_schema:
        passes.append("Schema markup present")
    else:
        warnings.append("No schema/structured data detected")

    if viewport and "width=device-width" in viewport:
        passes.append("Mobile viewport configured")
    elif viewport:
        warnings.append(f"Viewport set but may not be mobile-optimal: {viewport}")
    else:
        issues.append("Missing viewport meta tag - not mobile friendly")

    return {
        "meta_title": meta_title,
        "meta_title_length": len(meta_title),
        "meta_description": meta_desc,
        "meta_description_length": len(meta_desc),
        "og_title": og_title,
        "og_description": og_desc,
        "og_image": og_image,
        "canonical": canonical,
        "has_schema": has_schema,
        "robots": robots,
        "viewport": viewport,
        "issues": issues,
        "warnings": warnings,
        "passes": passes,
    }


def analyze_content(content_html: str) -> dict:
    issues = []
    warnings = []
    passes = []

    parser = HTMLContentParser()
    parser.feed(content_html)

    text = re.sub(r"<[^>]+>", " ", content_html)
    text = re.sub(r"\s+", " ", text).strip()
    word_count = len(text.split())

    if word_count < 300:
        issues.append(f"Very thin content ({word_count} words, aim for 1000+)")
    elif word_count < 1000:
        warnings.append(f"Content could be longer ({word_count} words, aim for 1500+)")
    else:
        passes.append(f"Good content length ({word_count} words)")

    h1s = parser.headings["h1"]
    h2s = parser.headings["h2"]
    h3s = parser.headings["h3"]

    if len(h1s) == 0:
        warnings.append("No H1 heading found in content")
    elif len(h1s) > 1:
        warnings.append(f"Multiple H1 headings ({len(h1s)}) - should have exactly 1")
    else:
        passes.append("Single H1 heading present")

    if len(h2s) == 0:
        warnings.append("No H2 headings - add subheadings for better structure")
    elif len(h2s) < 3:
        warnings.append(f"Only {len(h2s)} H2 headings - consider adding more for long content")
    else:
        passes.append(f"Good heading structure ({len(h2s)} H2s, {len(h3s)} H3s)")

    images_without_alt = [img for img in parser.images if not img["alt"].strip()]
    if parser.images:
        if images_without_alt:
            issues.append(f"{len(images_without_alt)}/{len(parser.images)} images missing alt text")
        else:
            passes.append(f"All {len(parser.images)} images have alt text")
    else:
        warnings.append("No images found - consider adding visual content")

    internal_links = [l for l in parser.links if settings.WP_URL in l["href"] or l["href"].startswith("/")]
    external_links = [l for l in parser.links if l["href"].startswith("http") and settings.WP_URL not in l["href"]]

    if not internal_links:
        warnings.append("No internal links found")
    else:
        passes.append(f"{len(internal_links)} internal links")

    if not external_links:
        warnings.append("No external links found")
    else:
        passes.append(f"{len(external_links)} external links")

    return {
        "word_count": word_count,
        "headings": {"h1": len(h1s), "h2": len(h2s), "h3": len(h3s), "h4": len(parser.headings["h4"])},
        "images_total": len(parser.images),
        "images_missing_alt": len(images_without_alt),
        "internal_links": len(internal_links),
        "external_links": len(external_links),
        "total_links": len(parser.links),
        "issues": issues,
        "warnings": warnings,
        "passes": passes,
    }


async def check_broken_links(links: list, base_url: str) -> list:
    broken = []
    checked = set()
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        for link_info in links[:50]:
            href = link_info.get("href", "") if isinstance(link_info, dict) else link_info
            if not href or href in checked:
                continue
            checked.add(href)
            if href.startswith("/"):
                href = base_url.rstrip("/") + href
            if not href.startswith("http"):
                continue
            try:
                resp = await client.head(href)
                if resp.status_code >= 400:
                    resp2 = await client.get(href)
                    if resp2.status_code >= 400:
                        broken.append({"url": href, "status": resp2.status_code})
            except Exception as e:
                broken.append({"url": href, "error": str(e)[:100]})
    return broken


async def fetch_all_pages() -> list:
    pages = []
    page_num = 1
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            resp = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/pages",
                headers=WP_HEADERS,
                params={"per_page": 50, "page": page_num, "status": "publish"}
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            pages.extend(batch)
            if len(batch) < 50:
                break
            page_num += 1
    return pages


async def fetch_all_posts() -> list:
    posts = []
    page_num = 1
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            resp = await client.get(
                f"{settings.WP_URL}/wp-json/wp/v2/posts",
                headers=WP_HEADERS,
                params={"per_page": 50, "page": page_num, "status": "publish"}
            )
            if resp.status_code != 200:
                break
            batch = resp.json()
            if not batch:
                break
            posts.extend(batch)
            if len(batch) < 50:
                break
            page_num += 1
    return posts


async def audit_single_page(page: dict) -> dict:
    title_raw = page.get("title", {}).get("rendered", "")
    title = re.sub(r"<[^>]+>", "", title_raw).strip()
    content = page.get("content", {}).get("rendered", "")
    link = page.get("link", "")
    page_id = page.get("id", 0)

    meta_result = await analyze_meta(page)
    content_result = analyze_content(content)

    all_links = []
    parser = HTMLContentParser()
    parser.feed(content)
    all_links = parser.links

    broken = await check_broken_links(all_links, settings.WP_URL)

    all_issues = meta_result["issues"] + content_result["issues"]
    all_warnings = meta_result["warnings"] + content_result["warnings"]
    all_passes = meta_result["passes"] + content_result["passes"]

    if broken:
        all_issues.append(f"{len(broken)} broken links found")

    total_checks = len(all_issues) + len(all_warnings) + len(all_passes)
    score = round((len(all_passes) / max(total_checks, 1)) * 100)

    return {
        "page_id": page_id,
        "title": title,
        "url": link,
        "slug": page.get("slug", ""),
        "score": score,
        "meta": meta_result,
        "content": content_result,
        "broken_links": broken,
        "issues_count": len(all_issues),
        "warnings_count": len(all_warnings),
        "passes_count": len(all_passes),
        "issues": all_issues,
        "warnings": all_warnings,
        "passes": all_passes,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_full_audit() -> dict:
    logger.info("Starting full SEO audit...")
    pages = await fetch_all_pages()
    posts = await fetch_all_posts()
    all_content = pages + posts

    results = []
    total_score = 0
    total_issues = 0
    total_warnings = 0

    for item in all_content:
        result = await audit_single_page(item)
        results.append(result)
        total_score += result["score"]
        total_issues += result["issues_count"]
        total_warnings += result["warnings_count"]

    avg_score = round(total_score / max(len(results), 1))

    results.sort(key=lambda x: x["score"])

    summary = {
        "total_pages": len(results),
        "average_score": avg_score,
        "total_issues": total_issues,
        "total_warnings": total_warnings,
        "pages_with_issues": sum(1 for r in results if r["issues_count"] > 0),
        "pages_perfect": sum(1 for r in results if r["issues_count"] == 0 and r["warnings_count"] == 0),
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }

    logger.info(f"Audit complete: {len(results)} pages, avg score {avg_score}%, {total_issues} issues")
    return summary
