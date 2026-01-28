import re
import sys
from datetime import datetime, timezone
from dateutil import parser as dtparser

import requests
from feedgen.feed import FeedGenerator
from pdfminer.high_level import extract_text


FEED_URL = "https://www.courtlistener.com/feed/court/scotus/"  # Atom
CHANNEL_LINK = "https://www.courtlistener.com/court/scotus/"
TITLE = "SCOTUS (CourtListener) + Full Text"
DESC = "Latest SCOTUS items with extracted PDF text."

UA = "scotus-rss-bot/1.0 (+https://github.com/)"

import re

# XML 1.0 valid chars:
#   #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
_invalid_xml_10 = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]"
)

def xml_safe(s: str) -> str:
    if s is None:
        return ""
    return _invalid_xml_10.sub("", str(s))

def fetch(url: str) -> requests.Response:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    return r

def atom_entries(atom_xml: str, max_items: int):
    # very small atom parser via regex (keeps deps minimal)
    # Each <entry>...</entry>
    entries = re.findall(r"<entry\b.*?</entry>", atom_xml, flags=re.S | re.I)
    return entries[:max_items]

def tag_text(entry: str, tag: str):
    m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", entry, flags=re.S | re.I)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()

def pick_link(entry: str, rel: str = None, type_: str = None):
    # <link rel="alternate" href="..."/>
    # <link rel="enclosure" type="application/pdf" href="..."/>
    links = re.findall(r"<link\b[^>]*?>", entry, flags=re.I)
    for lk in links:
        rel_m = re.search(r'rel="([^"]+)"', lk, flags=re.I)
        type_m = re.search(r'type="([^"]+)"', lk, flags=re.I)
        href_m = re.search(r'href="([^"]+)"', lk, flags=re.I)
        if not href_m:
            continue
        if rel and (not rel_m or rel_m.group(1) != rel):
            continue
        if type_ and (not type_m or type_m.group(1) != type_):
            continue
        return href_m.group(1)
    return ""

def find_pdf_on_html(opinion_url: str):
    html = fetch(opinion_url).text
    m = re.search(r'href="([^"]+\.pdf[^"]*)"', html, flags=re.I)
    if not m:
        return ""
    href = m.group(1)
    if href.startswith("http"):
        return href
    return "https://www.courtlistener.com" + href

def pdf_to_text(pdf_url: str):
    pdf_bytes = fetch(pdf_url).content
    # pdfminer can read from bytes via temp file-like using io.BytesIO
    import io
    return extract_text(io.BytesIO(pdf_bytes)) or ""

def normalize(s: str):
    s = (s or "").replace("\x00", "").replace("\r", "")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    return s.strip()

def main(max_items: int):
    atom_xml = fetch(FEED_URL).text

    fg = FeedGenerator()
    fg.id(CHANNEL_LINK)
    fg.title(TITLE)
    fg.link(href=CHANNEL_LINK, rel="alternate")
    fg.description(DESC)
    fg.language("en-us")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for entry in atom_entries(atom_xml, max_items):
        title = xml_safe(tag_text(entry, "title") or "Untitled")
        published = tag_text(entry, "published") or tag_text(entry, "updated")

        dt = dtparser.parse(published).astimezone(timezone.utc) if published else datetime.now(timezone.utc)

        opinion_url = pick_link(entry, rel="alternate") or ""
        pdf_url = pick_link(entry, rel="enclosure", type_="application/pdf") or ""

        scrape_error = ""
        full_text = ""
        link = opinion_url or CHANNEL_LINK

        try:
            if not pdf_url and opinion_url:
                pdf_url = find_pdf_on_html(opinion_url)
            if not pdf_url:
                raise RuntimeError("No PDF link found.")

            full_text = normalize(pdf_to_text(pdf_url))
            if len(re.sub(r"\s+", " ", full_text)) < 200:
                raise RuntimeError("Extracted text too small (likely scanned PDF).")
        except Exception as e:
            scrape_error = str(e)
        
        full_text = xml_safe(full_text)
        scrape_error = xml_safe(scrape_error)
        link = xml_safe(link)
        pdf_url = xml_safe(pdf_url)

        fe = fg.add_entry()
        fe.id(link)
        fe.title(title)
        fe.link(href=link)
        fe.pubDate(dt)

        parts = [f'<p><a href="{link}">Source</a></p>']
        if pdf_url:
            parts.append(f'<p><a href="{pdf_url}">PDF</a></p>')
        if scrape_error:
            parts.append(f"<p><b>Error:</b> {scrape_error}</p>")
        parts.append(f"<pre>{full_text}</pre>")

        fe.description(xml_safe("\n".join(parts)))

    rss = fg.rss_str(pretty=True).decode("utf-8")
    with open("feed.xml", "w", encoding="utf-8") as f:
        f.write(rss)

if __name__ == "__main__":
    x = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    main(x)
