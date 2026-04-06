import json
import os
import requests
import feedparser
from datetime import datetime
import re
from collections import defaultdict, Counter
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from openai import OpenAI
from playwright.sync_api import sync_playwright

# ---------------------------
# CONFIG
# ---------------------------

HEADERS = {"User-Agent": "Mozilla/5.0"}
session = requests.Session()
session.headers.update(HEADERS)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
CHEAP_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
TOP_STORY_MODEL = os.getenv("TOP_STORY_MODEL", "gpt-5.4")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

RSS_SOURCES = [
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
    ("Al Monitor", "https://www.al-monitor.com/rss"),
    ("Guardian", "https://www.theguardian.com/world/middleeast/rss"),

    # NEW
    ("Haaretz", "https://www.haaretz.com/cmlink/1.628752"),
    ("War on the Rocks", "https://warontherocks.com/feed/"),
    ("Responsible Statecraft", "https://responsiblestatecraft.org/feed/"),
]

MAX_PER_SOURCE = {
    # Regional priority
    "Amwaj": 3,
    "Jadaliyya": 3,
    "Middle East Eye": 3,
    "Al Monitor": 3,
    "Al Jazeera": 3,

    # Mixed / intl
    "Guardian": 2,
    "Haaretz": 2,
    "Carnegie ME": 2,

    # Controlled sources
    "War on the Rocks": 1,
    "Responsible Statecraft": 1,

    "default": 2
}

TOP_N = 8
REGIONAL_N = 8
DEEP_N = 16

AMWAJ_SEED_URL = "https://amwaj.media/en/media-monitor/tehran-vows-regional-escalation-after-trump-threatens-iranian-power-grid"
AMWAJ_MAX_ARTICLES = 20

JADALIYYA_SEED_URL = "https://www.jadaliyya.com/"
JAD_MAX_ARTICLES = 15

CARNEGIE_DIWAN_URL = "https://carnegieendowment.org/middle-east/diwan"
CARNEGIE_MAX_ARTICLES = 12

# ---------------------------
# CLEAN
# ---------------------------

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<.*?>", "", text)
    return re.sub(r"\s+", " ", text).strip()

def truncate(text, length=240):
    return text[:length].rsplit(" ", 1)[0] + "..." if len(text) > length else text

def clean_text(text):
    return " ".join(text.split()) if text else ""

def clean_title(t):
    return re.sub(r":.*", "", t).strip().capitalize() if t else t


# ---------------------------
# AMWAJ DATE PARSING
# ---------------------------

def extract_amwaj_date(article):
    url = article.get("link", "")

    # Amwaj URLs often include YYYY/MM/DD
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    if m:
        y, mth, d = m.groups()
        return f"{y}-{mth}-{d}"

    return "1970-01-01"  # fallback

def parse_amwaj_date(article):
    raw = article.get("date", "")

    try:
        return datetime.strptime(raw, "%b %d, %Y")
    except:
        return datetime(1970, 1, 1)

def get_latest_amwaj_sitrep():
    try:
        r = session.get("https://amwaj.media/en", timeout=10)
        soup = BeautifulSoup(r.text, "lxml")

        for a in soup.find_all("a", href=True):
            href = a["href"]

            if "sitrep" not in href.lower():
                continue

            text = a.get_text(separator=" ", strip=True)

            # 🔥 Must contain BOTH date + Sitrep title
            if "sitrep" not in text.lower():
                continue

            m = re.search(r"[A-Z][a-z]{2}\.\s\d{1,2},\s\d{4}", text)

            if not m:
                continue

            date_text = m.group(0)

            # Extract title cleanly
            title = text.replace(date_text, "").strip()

            # Normalize URL
            if not href.startswith("http"):
                href = "https://amwaj.media" + href

            return {
                "source": "Amwaj",
                "title": title,
                "summary": title,
                "link": href,
                "date": date_text
            }

    except Exception as e:
        print("[AMWAJ SITREP ERROR]", e)
        return None

    return None

# ---------------------------
# CARNEGIE HELPERS
# ---------------------------

def is_carnegie_diwan(article):
    return (
        article.get("source") == "Carnegie ME"
        and "/middle-east/diwan/" in article.get("link", "")
    )

def extract_carnegie_date_from_url(url):
    m = re.search(r"/(\d{4})/(\d{2})/", url)
    if not m:
        return ""
    y, mth = m.groups()
    return f"{y}-{mth}-01"

# ---------------------------
# RELEVANCE
# ---------------------------

KEY_TERMS = [
    "iran", "israel", "gaza", "hezbollah", "hamas", "tehran", "gulf",
    "saudi", "uae", "yemen", "iraq", "syria", "lebanon", "hormuz",
    "middle east", "missile", "drone", "energy", "oil"
]

def is_relevant(article):
    text = (article["title"] + " " + article.get("summary","")).lower()
    url = article.get("link", "").lower()

    # Must include at least one core regional anchor
    core = [
        "iran","israel","gaza","hezbollah","hamas","tehran",
        "saudi","uae","yemen","iraq","syria","lebanon","hormuz",
        "middle east"
    ]

    # Exclude obvious non-region geopolitical topics
    excluded = ["taiwan", "south china sea", "ukraine", "korea"]

    if any(e in text for e in excluded):
        return False

    # Allow Carnegie Diwan through, but require light regional signal
    if article.get("source") == "Carnegie ME":
        

        if "/middle-east/diwan/" in url:
            if any(k in text for k in KEY_TERMS):
                return True

    # Existing core logic
    if not any(k in text for k in core):
        return False

    return True

# ---------------------------
# LOW SIGNAL
# ---------------------------

def is_low_signal(article):
    bad = ["what you need", "latest", "live", "explainer", "analysis:", "how", "what is", "why"]
    return any(b in article["title"].lower() for b in bad)

def is_podcast(article):
    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()

    return (
        "podcast" in title
        or "podcast" in summary
        or "episode" in title
    )

def is_non_analysis(article):
    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()
    url = article.get("link", "").lower()

    bad_patterns = [
        "teach-in",
        "webinar",
        "register",
        "join us",
        "conference",
        "panel",
        "discussion"
    ]

    if any(p in title or p in summary for p in bad_patterns):
        return True

    # Carnegie-specific structural junk
    if any(x in url for x in ["/events/", "/collections/", "/projects/", "/programs-and-projects/"]):
        return True

    return False

def is_geopolitically_relevant(article):
    text = (
        article.get("title", "").lower() + " " +
        article.get("summary", "").lower()
    )

    relevant_terms = [
        "iran", "israel", "gaza", "lebanon", "hezbollah",
        "hamas", "iraq", "syria", "middle east", "gulf",
        "saudi", "uae", "qatar", "yemen", "houthi",
        "strait of hormuz", "hormuz", "tehran", "jerusalem"
    ]

    return any(term in text for term in relevant_terms)

def is_valid_article(article):
    if is_non_analysis(article):
        return False

    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()
    url = article.get("link", "").lower()
    source = article.get("source", "")

    # 🔥 HARD FILTER: remove MEE live blog entries (robust)
    if "middleeasteye.net/live-blog/" in url:
            return False
            
    bad_terms = [
        "about",
        "cookie",
        "privacy",
        "terms",
        "consent",
        "subscribe",
        "newsletter",
        "sign up",
        "login",
        "account"
    ]

    # 🚫 Title-based junk
    if any(b in title for b in bad_terms):
        return False

    # 🚫 URL-based junk
    if any(b in url for b in ["about", "cookie", "privacy", "terms"]):
        return False

    # 🚫 Explicit cookie/consent language in summary
    if "cookie" in summary or "consent" in summary:
        return False

    # 🚫 Extremely generic titles (common for junk pages)
    if len(title.strip()) < 8:
        return False

    # 🚫 Weak summaries
    if source == "Carnegie ME":
        if len(summary.strip()) < 30:
            return False
    else:
        if len(summary.strip()) < 60:
            return False

    # 🔥 Source-specific rule (high impact)
    if source == "Amwaj":
        if "cookie" in url or "consent" in url:
            return False

    return True

def classify_article(article):
    src = article.get("source", "")
    title = article.get("title", "").lower()

    # --- Hard rules first ---

    # Podcasts → exclude for now
    if is_podcast(article):
        return "exclude"

    # Jadaliyya should NEVER be Top Developments
    if src == "Jadaliyya":
        return "deep"


    # --- Event detection ---
    event_keywords = [
        "killed", "attack", "strike", "bomb", "missile",
        "clash", "explosion", "fire", "raid"
    ]

    if any(k in title for k in event_keywords):
        return "event"

    # --- Deep analysis sources ---
    if src in ["War on the Rocks", "Responsible Statecraft"]:
        return "deep"

    # --- Default ---
    return "regional"

# ---------------------------
# EVENT DETECTION
# ---------------------------

def classify_event(article):
    t = article["title"].lower()
    src = article.get("source", "")

    strong = [
        "strike", "attack", "missile", "killed", "explosion", "drone", "clashes",
        "threatens", "warns", "launches", "hits", "escalates", "deploys",
        "talks", "ceasefire", "negotiations", "crisis", "tensions", "conflict"
    ]

    # 🚫 NEVER treat these sources as "events"
    if src in ["Jadaliyya", "War on the Rocks", "Responsible Statecraft"]:
        return False

    # Strong keyword match
    if any(s in t for s in strong):
        return True

    # 🔥 Guardian-specific fallback (important)
    if src == "Guardian":
        return True

    return False

# ---------------------------
# SUMMARY + EDITORIAL WEIGHTING
# ---------------------------
# 🔒 LOCKED FUNCTION — DO NOT MODIFY
# Controls summary + "why it matters" behavior
def summarize(article):

    if client:
        try:
            r = client.chat.completions.create(
                model=CHEAP_MODEL,
                messages=[{
                    "role": "user",
                    "content": f"""
Return JSON:
{{"summary":"","importance":0-100,"why":""}}

TITLE: {article['title']}
TEXT: {article['summary']}
"""
                }]
            )
            parsed = json.loads(re.search(r"\{.*\}", r.choices[0].message.content, re.DOTALL).group())
            article["ai_summary"] = parsed.get("summary", "")
            article["importance"] = parsed.get("importance", 50)
            article["why"] = parsed.get("why", "")
        except:
            article["importance"] = 50
            article["ai_summary"] = article["summary"]
    else:
        article["importance"] = 50
        article["ai_summary"] = article["summary"]

    src = article["source"]

    # 🔥 Editorial weighting
    if src == "Amwaj":
        article["importance"] += 5
    elif src == "Jadaliyya":
        article["importance"] += 6
    elif src == "Carnegie ME":
        article["importance"] += 1
    elif src in ["Middle East Eye", "Al Monitor"]:
        article["importance"] += 3
    elif src == "Al Jazeera":
        article["importance"] += 1
    elif src in ["War on the Rocks", "Responsible Statecraft"]:
        article["importance"] -= 2
    elif src == "Guardian" and classify_event(article):
        article["importance"] += 3

    return article

# ---------------------------
# CLUSTERING
# ---------------------------

def extract_keywords(text):
    return set(re.findall(r"\b[a-z]{4,}\b", text.lower()))

def cluster_articles(articles):
    clusters, used = [], set()

    for i, a in enumerate(articles):
        if i in used:
            continue

        base = extract_keywords(a["title"])
        cluster = [a]
        used.add(i)

        for j, b in enumerate(articles[i + 1:], i + 1):
            if j in used:
                continue
            if len(base & extract_keywords(b["title"])) >= 3:
                cluster.append(b)
                used.add(j)

        clusters.append(cluster)

    return clusters

def select_representatives(clusters):
    return [max(c, key=lambda x: x["importance"]) for c in clusters]

# ---------------------------
# AMWAJ
# ---------------------------

def extract_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    return [urljoin(base, a["href"]) for a in soup.find_all("a", href=True) if "/en/" in a["href"]]

# 🔒 LOCKED FUNCTION — ingestion logic (fragile)

def crawl_amwaj():
    visited = set()
    queue = [
        ("https://amwaj.media/en/region/iran", 0),
        (AMWAJ_SEED_URL, 0)
    ]
    results = []

    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_page()

        while queue and len(results) < AMWAJ_MAX_ARTICLES:
            url, _ = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            try:
                page.goto(url)
                page.wait_for_timeout(2000)
                soup = BeautifulSoup(page.content(), "lxml")

                title = soup.find("h1")
                ps = soup.select("p")

                if title and ps:

                    if not any(x in url for x in ["/article/", "/media-monitor/"]):
                        continue

                    # --- extract date from page text ---
                    date_text = ""

                    for el in soup.find_all(string=True):
                        txt = clean_text(el)
                        if re.match(r"[A-Z][a-z]{2}\.\s\d{1,2},\s\d{4}", txt):
                            date_text = txt
                            break

                    results.append({
                        "source": "Amwaj",
                        "title": clean_text(title.get_text()),
                        "summary": truncate(" ".join(p.get_text() for p in ps[:5])),
                        "link": url,
                        "date": date_text
                    })

                for l in extract_links(page.content(), url):

                    # 🔥 Only follow Amwaj content paths
                    if not any(x in l for x in ["/article/", "/media-monitor/", "/region/"]):
                        continue

                    if l not in visited:
                        queue.append((l, 1))

            except:
                continue

    print(f"[AMWAJ] crawled: {len(results)}")
    return results

def get_latest_amwaj_sitrep_from_articles(amwaj_articles):
    sitreps = [
        a for a in amwaj_articles
        if a.get("source") == "Amwaj"
        and "sitrep" in a.get("title", "").lower()
    ]

    if not sitreps:
        return None

    def sort_key(a):
        raw = a.get("date", "")
        try:
            return datetime.strptime(raw, "%b. %d, %Y")
        except:
            try:
                return datetime.strptime(raw, "%b %d, %Y")
            except:
                return datetime(1970, 1, 1)

    sitreps.sort(key=sort_key, reverse=True)
    return sitreps[0]

# ---------------------------
# JADALIYYA
# ---------------------------

# 🔒 LOCKED FUNCTION — ingestion logic (fragile)
def crawl_jadaliyya():
    results = []

    with sync_playwright() as p:
        page = p.chromium.launch(headless=True).new_page()
        page.goto(JADALIYYA_SEED_URL)
        page.wait_for_timeout(3000)

        soup = BeautifulSoup(page.content(), "lxml")

        links = list(set([
            urljoin(JADALIYYA_SEED_URL, a["href"])
            for a in soup.find_all("a", href=True)
            if "/Details/" in a["href"]
        ]))[:JAD_MAX_ARTICLES]

    print(f"[JAD] links: {len(links)}")

    for url in links:
        try:
            s = BeautifulSoup(session.get(url).text, "lxml")
            title = s.find("h1")
            ps = s.select("p")

            if title and ps:
                results.append({
                    "source": "Jadaliyya",
                    "title": clean_text(title.get_text()),
                    "summary": truncate(" ".join(p.get_text() for p in ps[:6])),
                    "link": url
                })
        except:
            continue

    print(f"[JAD] crawled: {len(results)}")
    return results

# ---------------------------
# CARNEGIE DIWAN
# ---------------------------

def crawl_carnegie_diwan():
    results = []
    seen = set()

    try:
        print("[CARNEGIE] loading diwan")
        r = session.get(CARNEGIE_DIWAN_URL, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]

            if not href.startswith("http"):
                href = urljoin("https://carnegieendowment.org", href)

            if "/middle-east/diwan/" not in href:
                continue

            if any(x in href for x in ["/collections/", "/events/", "/search", "/people/"]):
                continue

            if not re.search(r"/middle-east/diwan/\d{4}/\d{2}/", href):
                continue

            if href not in seen:
                seen.add(href)
                links.append(href)

        print(f"[CARNEGIE] candidate links found: {len(links)}")

    except Exception as e:
        print("[CARNEGIE ERROR]", e)
        return results

    for url in links[:CARNEGIE_MAX_ARTICLES]:
        try:
            page = session.get(url, timeout=15)
            psoup = BeautifulSoup(page.text, "lxml")
            text = psoup.get_text(" ", strip=True)

            title = ""
            summary = ""
            date_text = ""

            # Title
            if psoup.find("h1"):
                title = clean_text(psoup.find("h1").get_text())

            if not title:
                og = psoup.find("meta", attrs={"property": "og:title"})
                if og and og.get("content"):
                    title = clean_text(og["content"])

            # Summary
            meta_desc = psoup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                summary = clean_text(meta_desc["content"])

            if not summary:
                og_desc = psoup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    summary = clean_text(og_desc["content"])

            if not summary and title:
                m = re.search(
                    re.escape(title) + r"\s+(.*?)\s+By\s+",
                    text,
                    flags=re.DOTALL
                )
                if m:
                    summary = clean_text(m.group(1))

            if not summary:
                paras = [clean_text(p.get_text(" ", strip=True)) for p in psoup.find_all("p")]
                paras = [p for p in paras if len(p) > 50]
                if paras:
                    summary = paras[0]

            # Date
            published = re.search(r"Published on ([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", text)
            if published:
                date_text = published.group(1)
            else:
                date_text = extract_carnegie_date_from_url(url)

            if title and summary:
                results.append({
                    "source": "Carnegie ME",
                    "title": title,
                    "summary": truncate(summary, 260),
                    "link": url,
                    "date": date_text
                })

        except Exception as e:
            print("[CARNEGIE ARTICLE ERROR]", url, e)
            continue

    print(f"[CARNEGIE] crawled: {len(results)}")
    return results

# ---------------------------
# HAARETZ
# ---------------------------

def crawl_haaretz():
    results = []

    try:
        print("[HAARETZ] loading homepage")

        r = session.get("https://www.haaretz.com/", timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        links = []

        for a in soup.find_all("a", href=True):
            href = a["href"]

            if "ty-article" in href:
                if href.startswith("/"):
                    href = "https://www.haaretz.com" + href

                links.append(href)

        print(f"[HAARETZ] candidate links found: {len(links)}")

    except Exception as e:
        print("[HAARETZ ERROR]", e)

    results = []

    for url in links[:10]:
        try:
            page = session.get(url, timeout=10)
            psoup = BeautifulSoup(page.text, "lxml")

            title_tag = psoup.find("h1")
            title = clean_text(title_tag.get_text()) if title_tag else url

        except:
            title = url

        results.append({
            "source": "Haaretz",
            "title": title,
            "summary": url,
            "link": url
        })

    print(f"[HAARETZ] returning {len(results)} articles")

    return results

# ---------------------------
# FETCH
# ---------------------------

def fetch_rss(name, url):
    items = []
    try:
        feed = feedparser.parse(session.get(url).content)
        for e in feed.entries[:15]:
            items.append({
                "source": name,
                "title": e.get("title", ""),
                "summary": truncate(clean_html(e.get("summary", ""))),
                "link": e.get("link", "")
            })
    except:
        pass
    return items

def fetch_all():
    items = []

    for n, u in RSS_SOURCES:
        items.extend([a for a in fetch_rss(n, u) if is_relevant(a)])

    items.extend([a for a in crawl_haaretz() if is_relevant(a)])

    # 🔥 Capture Amwaj separately
    amwaj_articles = [a for a in crawl_amwaj() if is_relevant(a)]

    # 🔥 Capture Jadaliyya separately
    jad_articles = [a for a in crawl_jadaliyya() if is_relevant(a)]

    # 🔥 Capture Carnegie Diwan separately
    carnegie_articles = [a for a in crawl_carnegie_diwan() if is_relevant(a)]

    # Add to main pool
    items.extend(amwaj_articles)
    items.extend(jad_articles)
    items.extend(carnegie_articles)

    return items, amwaj_articles

# ---------------------------
# BALANCE
# ---------------------------

def balance_section(articles, limit):
    selected = []
    counts = defaultdict(int)

    for a in articles:
        src = a["source"]
        if counts[src] >= MAX_PER_SOURCE.get(src, 2):
            continue
        selected.append(a)
        counts[src] += 1
        if len(selected) >= limit:
            break

    return selected

# ---------------------------
# BUILD
# ---------------------------

def build():
    raw, amwaj_articles = fetch_all()

    print("\nSOURCE COUNTS:", Counter([a["source"] for a in raw]))

    enriched = [
        summarize(a)
        for a in raw
        if not is_low_signal(a) and is_valid_article(a)
    ]
    enriched.sort(key=lambda x: x["importance"], reverse=True)

    clusters = cluster_articles(enriched)
    deduped = select_representatives(clusters)

    def is_amwaj_sitrep(a):
        return a["source"] == "Amwaj" and "sitrep" in a["title"].lower()

    def is_amwaj_deep_dive(a):
        return a["source"] == "Amwaj" and "deep dive" in a["title"].lower()

    # 🔥 PRIMARY: derive from crawled Amwaj articles
    latest_sitrep = get_latest_amwaj_sitrep_from_articles(amwaj_articles)

    # 🔁 FALLBACK: homepage scrape if needed
    if not latest_sitrep:
        latest_sitrep = get_latest_amwaj_sitrep()

    if latest_sitrep:
        latest_sitrep = summarize(latest_sitrep)

    events = [
        a for a in deduped
        if (
            classify_event(a)
            and not is_amwaj_deep_dive(a)
            and not is_amwaj_sitrep(a)
        )
    ]

    regional = [
        a for a in deduped
        if (
            not classify_event(a)
            and not is_amwaj_sitrep(a)
            and not is_amwaj_deep_dive(a)
            and a["source"] not in ["War on the Rocks", "Responsible Statecraft", "Guardian", "Jadaliyya", "Carnegie ME"]
        )
    ]

    # ---------------------------
    # DEEP ANALYSIS (RESTORED)
    # ---------------------------

    deep_candidates = [
        a for a in deduped
        if (
            not classify_event(a)
            and not is_amwaj_sitrep(a)
            and not is_podcast(a)
            and (
                is_geopolitically_relevant(a)
                or (a.get("source") == "Carnegie ME")
            )
            and not (
                a.get("source") == "War on the Rocks"
                and not is_geopolitically_relevant(a)
            )
        )
    ]

    # 🔥 Prefer analytical sources
    deep_candidates = sorted(
        deep_candidates,
        key=lambda x: (
            0 if x["source"] in ["Jadaliyya", "Guardian"] else
            1 if x["source"] == "Carnegie ME" else
            2 if x["source"] in ["War on the Rocks", "Responsible Statecraft"] else
            3,
            -x["importance"]
        )
    )

    # 🔥 CRITICAL: positional slice (restores old behavior)
    deep_slice = deep_candidates[:32]

    # Balance sources
    deep = balance_section(deep_slice, DEEP_N)

    # 🔥 Ensure Amwaj Deep Dives are included
    amwaj_deep = [a for a in deduped if is_amwaj_deep_dive(a)]

    for a in reversed(amwaj_deep):
        if a not in deep:
            deep.insert(0, a)

    # 🔥 Backfill
    if len(events) < TOP_N:
        for a in sorted(regional, key=lambda x: x["importance"], reverse=True):
            if a not in events:
                events.append(a)
            if len(events) >= TOP_N:
                break

    regional = [a for a in regional if a not in events]

    events = balance_section(events, TOP_N)
    regional = balance_section(regional, REGIONAL_N)

    # --- Ensure Haaretz representation AFTER balancing ---
    if not any(a["source"] == "Haaretz" for a in events + regional):
        haaretz_items = [a for a in enriched if a["source"] == "Haaretz"]
        if haaretz_items:
            regional.insert(0, haaretz_items[0])

    # --- Deduplicate by link (final safety pass) ---
    def dedupe_by_link(items):
        seen = set()
        unique = []
        for a in items:
            link = a.get("link")
            if link not in seen:
                seen.add(link)
                unique.append(a)
        return unique

    events = dedupe_by_link(events)
    regional = dedupe_by_link(regional)
    deep = dedupe_by_link(deep)

    # --- Cross-section dedupe (single-pass, stable) ---
    def dedupe_across_sections(events, regional, deep):
        seen = set()

        def keep(items):
            unique = []
            for a in items:
                link = a.get("link")
                if link not in seen:
                    seen.add(link)
                    unique.append(a)
            return unique

        events = keep(events)
        regional = keep(regional)
        deep = keep(deep)

        return events, regional, deep

    events, regional, deep = dedupe_across_sections(events, regional, deep)

    # 🔥 FINAL Sitrep anchoring (deterministic + safe)
    if latest_sitrep:

        def is_sitrep(a):
            return a.get("source") == "Amwaj" and "sitrep" in a.get("title", "").lower()

        # Remove ANY sitrep-like items (including malformed ones)
        events = [a for a in events if not is_sitrep(a)]

        # Ensure we maintain exact TOP_N size AFTER insertion
        if len(events) >= TOP_N:
            events = events[:TOP_N - 1]

        # Insert Sitrep at top
        events.insert(0, latest_sitrep)

        # 🔒 Final safety: enforce size exactly
        events = events[:TOP_N]

    print("FINAL TOP SOURCES:", [a["source"] for a in events])
    print("FINAL REGIONAL SOURCES:", [a["source"] for a in regional])
    print("FINAL DEEP SOURCES:", [a["source"] for a in deep])

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "top_story": generate_top_story(events, clusters, latest_sitrep),
        "top_developments": events,
        "regional_analysis": regional,
        "deep_analysis": deep
    }

# ---------------------------
# TOP STORY
# ---------------------------

# 🔒 LOCKED FUNCTION — DO NOT MODIFY
# Controls Top Story narrative quality
def generate_top_story(events, clusters, sitrep=None):

    print("[DEBUG] generating top story")

    fallback = "Regional developments indicate rising tensions."

    if not client or not clusters:
        return fallback

    cluster_text = "\n".join([
        " / ".join([clean_title(a["title"]) for a in c[:2]])
        for c in clusters[:3]
    ])

    try:
        r = client.chat.completions.create(
            model=TOP_STORY_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an analyst and regional expert on Middle Eastern affairs who synthesizes developments across the region with attention to political dynamics, "
                        "regional perspectives, and human impact. You avoid purely state-centric framing and focus on how events shape societies, economies, "
                        "and lived realities."
                    )
                },
                {
                    "role": "user",
                    "content": f"""
            Write a concise 3-4 sentence narrative intelligence briefing that reads as a single coherent paragraph. Sentences should not be overly long.

            STYLE:
            - Write in a smooth, natural narrative flow
            - Avoid filler transitions (e.g., "Meanwhile", "At the same time")
            - Use precise language
            - Be specific about actors, actions, and locations
            - Avoid generic or newsy phrasing
            - Avoid attributional phrasing (e.g., "reports that", "according to"); write with direct analytical voice

            GOAL:
            - Synthesize developments into a coherent regional picture
            - Reflect political dynamics alongside societal and human impact
            - Show how events are connected, not just occurring
            - End with a clear analytical takeaway

            INPUT:
            {cluster_text}
            """
                }
            ]
        )
        return r.choices[0].message.content.strip()

    except Exception as e:
        print("[TOP STORY ERROR]", e)
        return fallback

# ---------------------------
# SAVE
# ---------------------------

def save(data):
    os.makedirs("public", exist_ok=True)
    with open("public/briefing.json", "w") as f:
        json.dump(data, f, indent=2)

# ---------------------------
# MAIN
# ---------------------------

if __name__ == "__main__":
    briefing = build()
    save(briefing)
    print("Saved briefing.json")
