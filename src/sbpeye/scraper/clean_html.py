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

_ROW_NAV_PATTERNS = {
    "back to circular page",
    "back to main circular page",
    "home page",
    "back to home page",
}

# On the redesigned site (July 2026) a circular's content lives in a dedicated
# container; the surrounding chrome is plain <div>s, so scoping to this container is
# what removes navigation rather than the legacy table heuristics. Archive pages lack
# these containers and fall back to <body> + the legacy cleaning below.
CONTENT_CONTAINER_SELECTORS = ("div.pdfcontenttodownload", "div.circular-body")
# Print-only buttons, hidden holders, and white-on-white tracking numbers on detail pages.
NOISE_SELECTORS = (".no-print", "#automationPathHolder", 'font[color="#fff"]')


def _content_root(soup: BeautifulSoup):
    """Return (root, is_new_site).

    ``root`` is the circular's content container when the new site emits one (in which
    case ``is_new_site`` is True and the legacy chrome-stripping can be skipped), else
    the page ``<body>`` for archived table-based pages.
    """
    for selector in CONTENT_CONTAINER_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            return node, True
    return (soup.find("body") or soup), False


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip().lower()


def _is_navigation_row(row: Tag) -> bool:
    row_text = _normalize_text(row.get_text(" ", strip=True))
    if not row_text or len(row_text) > 160:
        return False

    if any(pattern in row_text for pattern in _ROW_NAV_PATTERNS):
        return True

    links = row.find_all("a")
    if not links or len(links) > 4:
        return False

    link_texts = {_normalize_text(link.get_text(" ", strip=True)) for link in links}
    link_hrefs = {link.get("href", "").strip().lower() for link in links}

    has_home_link = (
        any(text in {"home", "home page"} for text in link_texts)
        or any(href.endswith(("/index.asp", "/index.htm", "/index.html")) for href in link_hrefs)
    )
    has_circular_link = (
        any("circular page" in text for text in link_texts)
        or any("circulars/index" in href for href in link_hrefs)
    )
    return has_home_link and has_circular_link


def _is_navigation_link_group(tag: Tag) -> bool:
    text = _normalize_text(tag.get_text(" ", strip=True))
    if not text or len(text) > 120:
        return False
    return "home page" in text and "circular page" in text


def extract_sbp_text(html_content: bytes) -> str:
    """Extract readable text while preserving structural block boundaries."""
    soup = BeautifulSoup(html_content, "html.parser")
    for tag in soup.find_all(["head", "script", "style", "noscript", "template"]):
        tag.decompose()

    root, _is_new = _content_root(soup)
    for selector in NOISE_SELECTORS:
        for tag in root.select(selector):
            tag.decompose()
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

    body, is_new_site = _content_root(soup)
    if not body:
        return "<p>No content found</p>"

    for selector in NOISE_SELECTORS:
        for tag in body.select(selector):
            tag.decompose()

    # Archived (table-based) pages need the legacy navigation-chrome stripping; the new
    # site's content container is already clean, so this whole pass is skipped there.
    if not is_new_site:
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
                row_text = _normalize_text(tr.get_text(" ", strip=True))
                if not row_text:
                    tr.decompose()
                    continue
                if _is_navigation_row(tr):
                    tr.decompose()
                    continue
                imgs = tr.find_all("img")
                cells = tr.find_all(["td", "th"])
                if imgs and all(not td.get_text(strip=True) for td in cells):
                    tr.decompose()

        for table in body.find_all("table"):
            if not table.get_text(strip=True):
                table.decompose()

        for tag in body.find_all(["font", "span", "div", "p"]):
            if _is_navigation_link_group(tag):
                tag.decompose()

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
