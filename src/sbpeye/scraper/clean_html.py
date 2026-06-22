import re

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from urllib.parse import urljoin


NAV_PATTERNS = [
    "home", "what's new", "site map", "contact us", "faqs", "home page",
    "feedback", "careers", "tenders", "rti", "sitemap",
    "disclaimer", "copyright  ", "all rights reserved",
    "back to", "previous page", "next page", "last updated",
    "state bank of pakistan", "sbp logo",
    "i. i. chundrigar road", "phone:", "fax:",
]

_TEXT_BLOCKS = {
    "address", "article", "blockquote", "dd", "div", "dl", "dt", "figcaption",
    "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "main",
    "ol", "p", "pre", "section", "table", "tbody", "thead", "tfoot", "tr", "ul",
}


def extract_sbp_text(html_content: bytes) -> str:
    """Extract readable text while preserving structural block boundaries."""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(["head", "script", "style", "noscript", "template"]):
        tag.decompose()

    root = soup.find("body") or soup
    parts: list[str] = []

    def boundary() -> None:
        if parts and not parts[-1].endswith("\n\n"):
            parts.append("\n\n")

    def append_text(value: str) -> None:
        value = value.replace("\xa0", " ")
        if not value:
            return
        if (
            parts
            and parts[-1]
            and not parts[-1][-1].isspace()
            and not value[0].isspace()
            and parts[-1][-1] not in "([{/-"
            and value[0] not in ").,;:]}"
        ):
            parts.append(" ")
        parts.append(value)

    def walk(node) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            append_text(str(node))
            return
        if not isinstance(node, Tag):
            return
        if node.name == "br":
            parts.append("\n")
            return

        is_block = node.name in _TEXT_BLOCKS
        if is_block:
            boundary()
        for child in node.children:
            walk(child)
            if node.name in {"td", "th"} and child is not list(node.children)[-1]:
                append_text(" ")
        if node.name in {"td", "th"}:
            append_text(" | ")
        if is_block:
            boundary()

    walk(root)
    raw_text = "".join(parts)
    lines: list[str] = []
    blank = False
    for raw_line in raw_text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip(" |	")
        if line:
            lines.append(line)
            blank = False
        elif lines and not blank:
            lines.append("")
            blank = True
    text = "\n".join(lines).strip()
    text = re.sub(
        r"(?is)\bhome\s+about\s+sbp\s+publications\s+economic\s+data\s+"
        r"press\s+releases\s+circulars/notifications\b.*$",
        "",
        text,
    )
    return re.sub(
        r"(?is)\bbest\s+view\s+screen\s+resolution\s*:.*$", "", text
    ).strip()

def clean_sbp_html(html_content: bytes, base_url: str = "") -> str:
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup.find_all(["head", "script", "style", "noscript", "link", "meta"]):
        tag.decompose()

    if base_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and not src.startswith(("http://", "https://", "data:")):
                img["src"] = urljoin(base_url, src)
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if href and not href.startswith(("http://", "https://", "#", "javascript:", "mailto:")):
                a["href"] = urljoin(base_url, href)
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"

    body = soup.find("body")
    if not body:
        return "<p>No content found</p>"

    tables = body.find_all("table")
    for table in tables:
        text = table.get_text().strip().lower()
        if not text:
            table.decompose()
            continue

        links = table.find_all("a")
        link_count = len(links)

        if link_count >= 3 and len(text) < 500:
            if any(p in text for p in NAV_PATTERNS):
                table.decompose()
                continue

        if len(text) < 200 and any(p in text for p in NAV_PATTERNS):
            table.decompose()

    for table in body.find_all("table"):
        if not table.get_text(strip=True):
            table.decompose()
            continue
        for tr in table.find_all("tr"):
            row_text = tr.get_text(strip=True).lower()
            if not row_text:
                tr.decompose()
                continue
            # if len(row_text) < 100 and any(p in row_text for p in NAV_PATTERNS):
            #     tr.decompose()
            #     continue
            imgs = tr.find_all("img")
            cells = tr.find_all(["td", "th"])
            if imgs and all(not td.get_text(strip=True) for td in cells):
                tr.decompose()

    for font in soup.find_all("font"):
        font.unwrap()

    for tag in soup.find_all(style=True):
        del tag["style"]

    for tag in soup.find_all(bgcolor=True):
        del tag["bgcolor"]

    for tag in soup.find_all(background=True):
        del tag["background"]

    for tag in soup.find_all(color=True):
        del tag["color"]

    body_tag = soup.find("body")
    if body_tag:
        for attr in ("text", "link", "vlink", "alink"):
            body_tag.attrs.pop(attr, None)

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if any(kw in src.lower() for kw in ["logo", "banner", "header", "footer", "nav", "bg", "background", "back"]):
            img.decompose()

    for br in soup.find_all("br"):
        if br.parent and len(br.parent.get_text(strip=True)) == 0:
            br.decompose()

    result = str(body)
    return result
