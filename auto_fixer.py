import re
import logging
import base64
from html.parser import HTMLParser
import httpx
from config import settings

logger = logging.getLogger("seo-agent.fixer")

WP_AUTH = "Basic " + base64.b64encode(f"{settings.WP_USER}:{settings.WP_APP_PASSWORD}".encode()).decode()
WP_HEADERS = {"Authorization": WP_AUTH, "Content-Type": "application/json"}
WP = f"{settings.WP_URL}/wp-json/wp/v2"


class ImgFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def generate_meta_description(title: str, content_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", content_html)
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"[.!?]+", text)
    desc = ""
    for s in sentences:
        s = s.strip()
        if len(s) < 20:
            continue
        if len(desc) + len(s) + 2 <= 155:
            desc = f"{desc}. {s}" if desc else s
        else:
            break
    if len(desc) < 80:
        desc = f"{title} - Find the best products, reviews, and buying guides at Pet Hub Online."
    if len(desc) > 160:
        desc = desc[:157] + "..."
    return desc


def generate_alt_text(src: str, page_title: str) -> str:
    if not src:
        return f"{page_title} - Pet Hub Online"
    filename = src.split("/")[-1].split("?")[0]
    alt = re.sub(r"[-_]", " ", filename.rsplit(".", 1)[0])
    alt = re.sub(r"\d+x\d+", "", alt).strip()
    if len(alt) < 3:
        return f"{page_title} - Pet Hub Online"
    return alt.title()


async def fix_missing_meta_description(page: dict) -> dict:
    page_id = page["page_id"]
    title = page["title"]
    fixes = []

    if "Missing meta description" not in page.get("issues", []):
        return {"page_id": page_id, "fixes": [], "skipped": True}

    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in ["pages", "posts"]:
            r = await client.get(f"{WP}/{endpoint}/{page_id}?context=edit", headers=WP_HEADERS)
            if r.status_code != 200:
                continue

            data = r.json()
            content = data.get("content", {}).get("raw", "")
            desc = generate_meta_description(title, content)

            r2 = await client.post(
                f"{WP}/{endpoint}/{page_id}",
                headers=WP_HEADERS,
                json={
                    "meta": {"rank_math_description": desc},
                    "excerpt": desc,
                }
            )
            if r2.status_code == 200:
                fixes.append(f"Added meta description: {desc[:60]}...")
                logger.info(f"Fixed meta description for [{page_id}] {title}")
            break

    return {"page_id": page_id, "fixes": fixes}


async def fix_missing_alt_text(page: dict) -> dict:
    page_id = page["page_id"]
    title = page["title"]
    fixes = []

    has_alt_issue = any("images missing alt text" in i for i in page.get("issues", []))
    if not has_alt_issue:
        return {"page_id": page_id, "fixes": [], "skipped": True}

    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in ["pages", "posts"]:
            r = await client.get(f"{WP}/{endpoint}/{page_id}?context=edit", headers=WP_HEADERS)
            if r.status_code != 200:
                continue

            data = r.json()
            content = data.get("content", {}).get("raw", "")

            parser = ImgFinder()
            parser.feed(content)

            modified = content
            count = 0

            for img in parser.images:
                src = img.get("src", "")
                alt = img.get("alt", "")
                if alt.strip():
                    continue

                new_alt = generate_alt_text(src, title)
                idx = modified.find(f'src="{src}"')
                if idx < 0:
                    continue
                start = modified.rfind("<img", 0, idx)
                end = modified.find(">", idx) + 1
                if start < 0 or end <= 0:
                    continue
                img_tag = modified[start:end]
                if 'alt=""' in img_tag:
                    new_img = img_tag.replace('alt=""', f'alt="{new_alt}"', 1)
                    modified = modified[:start] + new_img + modified[end:]
                    count += 1

            media_ids = set(re.findall(r'wp-image-(\d+)', modified))
            for mid in media_ids:
                try:
                    mr = await client.get(f"{WP}/media/{mid}", headers=WP_HEADERS)
                    if mr.status_code == 200 and not mr.json().get("alt_text", "").strip():
                        mtitle = mr.json().get("title", {}).get("rendered", title)
                        await client.post(
                            f"{WP}/media/{mid}",
                            headers=WP_HEADERS,
                            json={"alt_text": f"{mtitle} - Pet Hub Online"}
                        )
                        count += 1
                except Exception:
                    pass

            if count > 0:
                r2 = await client.post(
                    f"{WP}/{endpoint}/{page_id}",
                    headers=WP_HEADERS,
                    json={"content": modified}
                )
                if r2.status_code == 200:
                    fixes.append(f"Fixed {count} missing alt texts")
                    logger.info(f"Fixed {count} alt texts for [{page_id}] {title}")
            break

    return {"page_id": page_id, "fixes": fixes}


class HeadingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headings = []
        self._tag = None
        self._text = ""
        self._pos = 0
        self._raw = ""

    def feed(self, data):
        self._raw = data
        super().feed(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._tag = tag
            self._text = ""
            self._pos = self.getpos()

    def handle_data(self, data):
        if self._tag:
            self._text += data

    def handle_endtag(self, tag):
        if tag == self._tag:
            self.headings.append({
                "tag": self._tag,
                "level": int(self._tag[1]),
                "text": self._text.strip(),
            })
            self._tag = None


def fix_heading_hierarchy(content: str) -> tuple:
    parser = HeadingParser()
    parser.feed(content)
    headings = parser.headings
    if not headings:
        return content, []

    fixes = []
    modified = content

    h1_count = sum(1 for h in headings if h["level"] == 1)
    if h1_count > 1:
        found_first = False
        for h in headings:
            if h["level"] == 1:
                if found_first:
                    old_open = f'<h1'
                    text = h["text"]
                    pattern = re.compile(
                        r'<h1([^>]*)>(.*?' + re.escape(text) + r'.*?)</h1>',
                        re.DOTALL
                    )
                    match = pattern.search(modified)
                    if match:
                        attrs = match.group(1)
                        inner = match.group(2)
                        modified = modified[:match.start()] + f'<h2{attrs}>{inner}</h2>' + modified[match.end():]
                        fixes.append(f"Demoted extra H1 '{text[:40]}' to H2")
                else:
                    found_first = True

    parser2 = HeadingParser()
    parser2.feed(modified)
    headings2 = parser2.headings

    prev_level = 1
    for h in headings2:
        level = h["level"]
        if level > prev_level + 1:
            correct_level = prev_level + 1
            text = h["text"]
            pattern = re.compile(
                r'<h' + str(level) + r'([^>]*)>(.*?' + re.escape(text) + r'.*?)</h' + str(level) + r'>',
                re.DOTALL
            )
            match = pattern.search(modified)
            if match:
                attrs = match.group(1)
                inner = match.group(2)
                modified = (modified[:match.start()] +
                           f'<h{correct_level}{attrs}>{inner}</h{correct_level}>' +
                           modified[match.end():])
                fixes.append(f"Fixed H{level} '{text[:40]}' -> H{correct_level} (was skipping levels)")
        prev_level = level if level <= prev_level + 1 else prev_level + 1

    return modified, fixes


async def fix_heading_structure(page: dict) -> dict:
    page_id = page["page_id"]
    title = page["title"]
    fixes = []

    has_heading_issue = any(
        "H1" in i or "H2" in i or "heading" in i.lower()
        for i in page.get("issues", []) + page.get("warnings", [])
    )
    if not has_heading_issue:
        return {"page_id": page_id, "fixes": [], "skipped": True}

    async with httpx.AsyncClient(timeout=15) as client:
        for endpoint in ["pages", "posts"]:
            r = await client.get(f"{WP}/{endpoint}/{page_id}?context=edit", headers=WP_HEADERS)
            if r.status_code != 200:
                continue

            data = r.json()
            content = data.get("content", {}).get("raw", "")
            if not content:
                break

            modified, heading_fixes = fix_heading_hierarchy(content)
            if heading_fixes and modified != content:
                r2 = await client.post(
                    f"{WP}/{endpoint}/{page_id}",
                    headers=WP_HEADERS,
                    json={"content": modified}
                )
                if r2.status_code == 200:
                    fixes.extend(heading_fixes)
                    logger.info(f"Fixed headings for [{page_id}] {title}: {len(heading_fixes)} changes")
            break

    return {"page_id": page_id, "fixes": fixes}


async def auto_fix_safe_issues(audit_results: list) -> dict:
    total_fixes = 0
    fix_details = []

    for page in audit_results:
        page_fixes = []

        result1 = await fix_missing_meta_description(page)
        if result1["fixes"]:
            page_fixes.extend(result1["fixes"])

        result2 = await fix_missing_alt_text(page)
        if result2["fixes"]:
            page_fixes.extend(result2["fixes"])

        result3 = await fix_heading_structure(page)
        if result3["fixes"]:
            page_fixes.extend(result3["fixes"])

        if page_fixes:
            fix_details.append({"page": page["title"], "page_id": page["page_id"], "fixes": page_fixes})
            total_fixes += len(page_fixes)

    return {
        "total_fixes": total_fixes,
        "pages_fixed": len(fix_details),
        "details": fix_details,
    }
