import json
import os
import requests
import feedparser
from datetime import datetime
import re
from collections import defaultdict, Counter
from urllib.parse import urljoin
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
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
    ("Al Monitor", "https://www.al-monitor.com/rss"),
    ("Guardian", "https://www.theguardian.com/world/middleeast/rss"),

    # NEW
    ("Haaretz", "https://www.haaretz.com/cmlink/1.628752"),
    ("War on the Rocks", "https://warontherocks.com/feed/"),
    ("Responsible Statecraft", "https://responsiblestatecraft.org/feed/"),
    ("Drop Site News", "https://www.dropsitenews.com/feed"),
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
    "Drop Site News": 2,

    "default": 2
}

TOP_N = 12
REGIONAL_N = 9
DEEP_N = 16

TOP_DEVELOPMENTS_MAX_AGE_DAYS = 3
TOP_DEVELOPMENTS_AMWAJ_MAX_AGE_DAYS = 5
TOP_DEVELOPMENTS_MIN_ITEMS = 9

TOP_DEVELOPMENTS_SOURCE_ORDER = [
    "Guardian",
    "Haaretz",
    "Middle East Eye",
    "Al Monitor",
    "Al Jazeera",
    "Amwaj",
    "Carnegie ME",
    "Drop Site News",
]

AMWAJ_SEED_URL = "https://amwaj.media/en/media-monitor/tehran-vows-regional-escalation-after-trump-threatens-iranian-power-grid"
AMWAJ_MAX_ARTICLES = 20
AMWAJ_DEEP_DIVE_MAX_AGE_DAYS = 30

JADALIYYA_SEED_URL = "https://www.jadaliyya.com/"
JAD_MAX_ARTICLES = 15

AL_JAZEERA_MIDDLE_EAST_URL = "https://www.aljazeera.com/middle-east/"
AL_JAZEERA_MAX_ARTICLES = 15

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

def debug_article(label, article):
    print(
        f"[DEBUG:{label}] "
        f"source={article.get('source')} | "
        f"title={article.get('title')} | "
        f"importance={article.get('importance', 'NA')} | "
        f"link={article.get('link')}"
    )
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

def parse_article_datetime(article):
    raw = article.get("published_at") or article.get("date") or ""

    if not raw:
        return None

    raw = raw.strip()

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except:
        pass

    for fmt in ("%b. %d, %Y", "%b %d, %Y", "%B %d, %Y", "%d %B %Y %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except:
            continue

    return None

def is_recent_top_development(article, now=None):
    max_age_days = (
        TOP_DEVELOPMENTS_AMWAJ_MAX_AGE_DAYS
        if article.get("source") == "Amwaj"
        else TOP_DEVELOPMENTS_MAX_AGE_DAYS
    )
    published = parse_article_datetime(article)

    if not published:
        print(f"[TOP RECENCY SKIP] missing date | {article.get('source')} | {article.get('title')}")
        return False

    now = now or datetime.now(timezone.utc)
    cutoff_date = now.date() - timedelta(days=max_age_days)
    is_recent = published.date() >= cutoff_date

    if not is_recent:
        print(
            f"[TOP RECENCY SKIP] {published.date()} older than {max_age_days}d | "
            f"{article.get('source')} | {article.get('title')}"
        )

    return is_recent

def is_video_item(article):
    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()
    link = article.get("link", article.get("url", "")).lower()

    video_markers = [
        "/video/",
        "-video",
        " video:",
        " – video",
        " - video",
        "newsfeed",
        "quotable",
        "watch:",
    ]

    return any(marker in f"{title} {summary} {link}" for marker in video_markers)

def is_live_wrapper_item(article):
    title = article.get("title", "").lower()
    link = article.get("link", article.get("url", "")).lower()

    return (
        "/live/" in link
        or "/live-blog/" in link
        or "live updates" in title
        or "as it happened" in title
    )

def is_top_development_candidate(article):
    return (
        is_recent_top_development(article)
        and not is_video_item(article)
        and not is_live_wrapper_item(article)
    )

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
    bad = ["what you need", "latest", "live", "explainer", "analysis:", "how"]
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

def jadaliyya_deep_adjustment(article):
    """
    Small Jadaliyya-specific scoring nudge for Deep Analysis.
    Favors essay-style analytical writing over podcast/live wrappers,
    while still allowing roundup/context pieces to remain competitive.
    """
    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()
    url = article.get("link", "").lower()

    text = f"{title} {summary} {url}"
    score = 0

    # Strong positive signals for essay-style analysis
    strong_positive = [
        "why ",
        "how ",
        "at a crossroads",
        "ideology",
        "war without limits",
        "wages of impunity",
        "neo-imperialism",
        "political economy",
        "infrastructure",
        "state-building",
        "occupation",
        "security",
        "breakdown",
        "arms industry",
    ]
    for phrase in strong_positive:
        if phrase in text:
            score += 2

    # Mild positive signals for framing/synthesis pieces
    mild_positive = [
        "roundup",
        "in context",
        "context",
        "essential readings",
        "new lessons",
    ]
    for phrase in mild_positive:
        if phrase in text:
            score += 1

    # Negative signals for media wrappers / event-like discussion formats
    negative = [
        "podcast",
        "podcasts",
        "series",
        "limited podcast series",
        "episode",
        "pilot episode",
        "interview",
        "interviews",
        "live ",
        "join us",
        "roundtable",
        "webinar",
        "teach-in",
        "conference",
        "panel",
        "discussion",
        "register",
    ]
    for phrase in negative:
        if phrase in text:
            score -= 3

    # Extra URL-based penalty for obvious media-wrapper pieces
    if "media-wars" in url or "iran-on-the-brink" in url:
        score -= 2

    return score    

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

    # 🔥 MEE broader event detection (UPGRADED)
    if src == "Middle East Eye":
        # Strong signals
        if any(k in t for k in [
            "strike", "attack", "kills", "killed",
            "rescues", "rescue", "shot down",
            "missile", "airstrike", "clashes",
            "raid"
        ]):
            return True

    # 🔥 Softer reporting language (CRITICAL ADD)
    if any(k in t for k in [
        "says", "vows", "announces", "claims",
        "reports", "warns", "accuses"
    ]):
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
    elif src == "Middle East Eye":
        article["importance"] += 3
    elif src == "Al Monitor":
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

                    published_at = None
                    if date_text:
                        try:
                            dt = datetime.strptime(date_text, "%b. %d, %Y").replace(tzinfo=timezone.utc)
                            published_at = dt.isoformat().replace("+00:00", "Z")
                        except:
                            try:
                                dt = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=timezone.utc)
                                published_at = dt.isoformat().replace("+00:00", "Z")
                            except:
                                published_at = None

                    results.append({
                        "source": "Amwaj",
                        "title": clean_text(title.get_text()),
                        "summary": truncate(" ".join(p.get_text() for p in ps[:5])),
                        "link": url,
                        "date": date_text,
                        "published_at": published_at
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

def is_recent_amwaj_deep_dive(article, now=None):
    published = parse_article_datetime(article)
    if not published:
        return False

    now = now or datetime.now(timezone.utc)
    return published.date() >= (now.date() - timedelta(days=AMWAJ_DEEP_DIVE_MAX_AGE_DAYS))

def fetch_amwaj_article(url):
    try:
        r = session.get(url, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")

        title_tag = soup.find("h1")
        title = clean_text(title_tag.get_text()) if title_tag else ""

        if not title:
            title = clean_text(soup.find("meta", attrs={"property": "og:title"}).get("content", "")) if soup.find("meta", attrs={"property": "og:title"}) else ""

        summary = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            summary = clean_text(meta_desc["content"])

        if not summary:
            og_desc = soup.find("meta", attrs={"property": "og:description"})
            if og_desc and og_desc.get("content"):
                summary = clean_text(og_desc["content"])

        if not summary:
            paras = [clean_text(p.get_text(" ", strip=True)) for p in soup.find_all("p")]
            paras = [p for p in paras if len(p) > 50]
            if paras:
                summary = " ".join(paras[:3])

        date_text = ""
        published_at = None
        m = re.search(r'PublishedAt\\":\\"([^\\"]+)', r.text)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                published_at = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                date_text = dt.strftime("%b. %-d, %Y")
            except:
                published_at = None

        page_text = soup.get_text(" ", strip=True)
        m = re.search(r"[A-Z][a-z]{2}\.\s\d{1,2},\s\d{4}", page_text)
        if not published_at and m:
            date_text = m.group(0)
        else:
            m = re.search(r"[A-Z][a-z]{2}\s\d{1,2},\s\d{4}", page_text)
            if not published_at and m:
                date_text = m.group(0)

        if date_text and not published_at:
            try:
                dt = datetime.strptime(date_text, "%b. %d, %Y").replace(tzinfo=timezone.utc)
                published_at = dt.isoformat().replace("+00:00", "Z")
            except:
                try:
                    dt = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=timezone.utc)
                    published_at = dt.isoformat().replace("+00:00", "Z")
                except:
                    published_at = None

        if not title or not summary:
            return None

        return {
            "source": "Amwaj",
            "title": title,
            "summary": truncate(summary, 260),
            "link": url,
            "date": date_text,
            "published_at": published_at
        }

    except Exception as e:
        print("[AMWAJ ARTICLE ERROR]", url, e)
        return None

def get_latest_amwaj_deep_dive():
    try:
        r = session.get("https://amwaj.media/en", timeout=15)
        hrefs = re.findall(r'href\\":\\"(/en/[^\\"]*deep-dive[^\\"]*)', r.text)
        if not hrefs:
            print("[AMWAJ DEEP DIVE] no embedded links found")
            return None

        seen = []
        for href in hrefs:
            url = urljoin("https://amwaj.media", href)
            if url not in seen:
                seen.append(url)

        candidates = []
        for url in seen[:6]:
            article = fetch_amwaj_article(url)
            if article and "deep dive" in article.get("title", "").lower():
                candidates.append(article)

        candidates = [a for a in candidates if is_recent_amwaj_deep_dive(a)]
        if not candidates:
            print("[AMWAJ DEEP DIVE] no recent candidates")
            return None

        candidates.sort(key=lambda a: parse_article_datetime(a) or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
        return candidates[0]

    except Exception as e:
        print("[AMWAJ DEEP DIVE ERROR]", e)
        return None

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
                published_at = None

                # Try to extract date from page text
                date_text = ""
                for el in s.find_all(string=True):
                    txt = clean_text(el)
                    if re.match(r"[A-Z][a-z]{2}\.\s\d{1,2},\s\d{4}", txt):
                        date_text = txt
                        break

                if date_text:
                    try:
                        dt = datetime.strptime(date_text, "%b. %d, %Y").replace(tzinfo=timezone.utc)
                        published_at = dt.isoformat().replace("+00:00", "Z")
                    except:
                        try:
                            dt = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=timezone.utc)
                            published_at = dt.isoformat().replace("+00:00", "Z")
                        except:
                            published_at = None

                results.append({
                    "source": "Jadaliyya",
                    "title": clean_text(title.get_text()),
                    "summary": truncate(" ".join(p.get_text() for p in ps[:6])),
                    "link": url,
                    "published_at": published_at
                })
        except Exception as e:
            print("[JAD ARTICLE ERROR]", url, e)
            continue

    print(f"[JAD] crawled: {len(results)}")
    return results

# ---------------------------
# MIDDLE EAST EYE
# ---------------------------

def crawl_middle_east_eye():
    results = []
    seen = set()

    BASE = "https://www.middleeasteye.net"

    sections = [
        "https://www.middleeasteye.net/news",
        "https://www.middleeasteye.net/analysis",
        "https://www.middleeasteye.net/opinion"
    ]

    try:
        print("[MEE] crawling sections")

        for section_url in sections:
            r = session.get(section_url, timeout=15)
            soup = BeautifulSoup(r.text, "lxml")

            for a in soup.find_all("a", href=True):
                href = a["href"]

                # Normalize URL
                if not href.startswith("http"):
                    href = BASE + href

                # 🔥 Only real articles
                if not any(x in href for x in ["/news/", "/analysis/", "/opinion/"]):
                    continue

                # 🚫 Skip live blogs
                if "/live-blog/" in href:
                    continue

                # 🚫 Skip duplicates
                if href in seen:
                    continue

                seen.add(href)

                # Limit initial fetch size
                if len(seen) >= 25:
                    break

        print(f"[MEE] candidate links: {len(seen)}")

    except Exception as e:
        print("[MEE SECTION ERROR]", e)
        return results

    # ---------------------------
    # Fetch article content
    # ---------------------------

    for url in list(seen)[:15]:
        try:
            page = session.get(url, timeout=15)
            soup = BeautifulSoup(page.text, "lxml")

            title_tag = soup.find("h1")
            title = clean_text(title_tag.get_text()) if title_tag else ""

            if not title:
                continue

            # Try meta description first (best quality)
            summary = ""
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                summary = clean_text(meta_desc["content"])

            # Fallback: first paragraphs
            if not summary:
                paras = [
                    clean_text(p.get_text(" ", strip=True))
                    for p in soup.find_all("p")
                ]
                paras = [p for p in paras if len(p) > 40]
                if paras:
                    summary = paras[0]

            if not summary:
                continue

            published_at = None

            # Try meta/article time first
            time_tag = soup.find("meta", attrs={"property": "article:published_time"})
            if time_tag and time_tag.get("content"):
                raw_time = clean_text(time_tag["content"])
                try:
                    dt = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
                    dt = dt.astimezone(timezone.utc)
                    published_at = dt.isoformat().replace("+00:00", "Z")
                except:
                    published_at = None

            # Fallback: look for visible date text
            if not published_at:
                date_text = ""
                page_text = soup.get_text(" ", strip=True)
                m = re.search(r"Published date:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4}\s+\d{1,2}:\d{2})", page_text)
                if m:
                    date_text = m.group(1)

                for el in soup.find_all(string=True):
                    if date_text:
                        break

                    txt = clean_text(el)
                    m = re.search(r"Published date:\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4}\s+\d{1,2}:\d{2})", txt)
                    if m:
                        date_text = m.group(1)
                        break

                    if re.match(r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}", txt):
                        date_text = txt
                        break

                if date_text:
                    try:
                        dt = datetime.strptime(date_text, "%b %d, %Y").replace(tzinfo=timezone.utc)
                        published_at = dt.isoformat().replace("+00:00", "Z")
                    except:
                        try:
                            dt = datetime.strptime(date_text, "%d %B %Y %H:%M").replace(tzinfo=timezone.utc)
                            published_at = dt.isoformat().replace("+00:00", "Z")
                        except:
                            published_at = None

            results.append({
                "source": "Middle East Eye",
                "title": title,
                "summary": truncate(summary, 260),
                "link": url,
                "published_at": published_at
            })

        except Exception as e:
            print("[MEE ARTICLE ERROR]", url, e)
            continue

    print(f"[MEE] crawled: {len(results)}")

    return results

# ---------------------------
# AL JAZEERA
# ---------------------------

def parse_al_jazeera_date(text):
    m = re.search(r"Published On\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text or "")
    if not m:
        return None

    day, month, year = m.groups()
    try:
        dt = datetime.strptime(f"{day} {month} {year}", "%d %b %Y").replace(tzinfo=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except:
        try:
            dt = datetime.strptime(f"{day} {month} {year}", "%d %B %Y").replace(tzinfo=timezone.utc)
            return dt.isoformat().replace("+00:00", "Z")
        except:
            return None

def crawl_al_jazeera_middle_east():
    results = []
    seen = set()
    base = "https://www.aljazeera.com"

    try:
        print("[AL JAZEERA] crawling Middle East section")
        r = session.get(AL_JAZEERA_MIDDLE_EAST_URL, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print("[AL JAZEERA SECTION ERROR]", e)
        return results

    links = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        if href.startswith("/"):
            href = urljoin(base, href)

        if not href.startswith(base):
            continue

        if href in seen:
            continue

        if not re.search(r"/(news|features|opinions)/\d{4}/\d{1,2}/\d{1,2}/", href):
            continue

        if any(x in href for x in ["/video/", "/liveblog/", "/program/", "/podcasts/"]):
            continue

        seen.add(href)
        links.append(href)

        if len(links) >= 25:
            break

    print(f"[AL JAZEERA] candidate links: {len(links)}")

    for url in links[:AL_JAZEERA_MAX_ARTICLES]:
        try:
            page = session.get(url, timeout=15)
            article_soup = BeautifulSoup(page.text, "lxml")

            title_tag = article_soup.find("h1")
            title = clean_text(title_tag.get_text()) if title_tag else ""

            if not title:
                continue

            summary = ""
            meta_desc = article_soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                summary = clean_text(meta_desc["content"])

            if not summary:
                og_desc = article_soup.find("meta", attrs={"property": "og:description"})
                if og_desc and og_desc.get("content"):
                    summary = clean_text(og_desc["content"])

            if not summary:
                paras = [
                    clean_text(p.get_text(" ", strip=True))
                    for p in article_soup.find_all("p")
                ]
                paras = [p for p in paras if len(p) > 40]
                if paras:
                    summary = paras[0]

            if not summary:
                continue

            published_at = None
            time_tag = article_soup.find("meta", attrs={"property": "article:published_time"})
            if time_tag and time_tag.get("content"):
                try:
                    dt = datetime.fromisoformat(clean_text(time_tag["content"]).replace("Z", "+00:00"))
                    published_at = dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                except:
                    published_at = None

            if not published_at:
                published_at = parse_al_jazeera_date(article_soup.get_text(" ", strip=True))

            results.append({
                "source": "Al Jazeera",
                "title": title,
                "summary": truncate(summary, 260),
                "link": url,
                "published_at": published_at
            })

        except Exception as e:
            print("[AL JAZEERA ARTICLE ERROR]", url, e)
            continue

    print(f"[AL JAZEERA] crawled: {len(results)}")
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

            published_at = None
            if date_text:
                try:
                    dt = datetime.strptime(date_text, "%B %d, %Y").replace(tzinfo=timezone.utc)
                    published_at = dt.isoformat().replace("+00:00", "Z")
                except:
                    try:
                        dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        published_at = dt.isoformat().replace("+00:00", "Z")
                    except:
                        published_at = None

            if title and summary:
                results.append({
                    "source": "Carnegie ME",
                    "title": title,
                    "summary": truncate(summary, 260),
                    "link": url,
                    "date": date_text,
                    "published_at": published_at
                })

        except Exception as e:
            print("[CARNEGIE ARTICLE ERROR]", url, e)
            continue

    print(f"[CARNEGIE] crawled: {len(results)}")
    return results
# ----------------------------
# REUTERS
# ----------------------------

# ----------------------------
# REUTERS
# ----------------------------

REUTERS_SOURCE_PAGES = [
    "https://www.reuters.com/world/middle-east/",
    "https://www.reuters.com/world/iran/",
    "https://www.reuters.com/world/israel-hamas/",
    "https://www.reuters.com/",
]

REUTERS_BACKFILL_MAX_AGE_HOURS = 72
REUTERS_BACKFILL_MAX_ITEMS = 4


def _parse_reuters_relative_time(text):
    text = (text or "").strip().lower()

    m = re.match(r"(\d+)\s+mins?\s+ago", text)
    if m:
        return datetime.now(timezone.utc) - timedelta(minutes=int(m.group(1)))

    m = re.match(r"(\d+)\s+hours?\s+ago", text)
    if m:
        return datetime.now(timezone.utc) - timedelta(hours=int(m.group(1)))

    return None

def _parse_reuters_url_date(link):
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", link or "")
    if not m:
        return None

    y, mth, d = m.groups()
    try:
        return datetime.strptime(f"{y}-{mth}-{d}", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except:
        return None


def _looks_like_reuters_live_item(title, link):
    title = (title or "").lower()
    link = (link or "").lower()

    if "/live/" in link:
        return True
    if title.startswith("live:") or "live updates" in title:
        return True

    return False


def _is_reuters_relevant_text(title, summary, link):
    text = " ".join([title or "", summary or "", link or ""]).lower()

    required_any = [
        "iran", "israel", "gaza", "lebanon", "hezbollah", "hormuz",
        "tehran", "tel aviv", "west bank", "middle east", "syria",
        "iraq", "yemen", "kuwait", "qatar", "uae", "saudi", "oman"
    ]
    return any(term in text for term in required_any)


def _reuters_dupish(title, link, existing_items):
    title = (title or "").strip().lower()
    link = (link or "").strip().lower()

    for item in existing_items:
        existing_title = (item.get("title") or "").strip().lower()
        existing_link = (item.get("url") or item.get("link") or "").strip().lower()

        if link and existing_link and link == existing_link:
            return True

        if title and existing_title:
            if title == existing_title:
                return True

            title_words = set(w for w in title.split() if len(w) > 3)
            existing_words = set(w for w in existing_title.split() if len(w) > 3)

            if title_words and existing_words:
                overlap = len(title_words & existing_words)
                if overlap >= 7:
                    return True

    return False


def get_reuters_backfill_candidates(existing_top_developments, limit=REUTERS_BACKFILL_MAX_ITEMS):
    now_utc = datetime.now(timezone.utc)
    candidates = []
    seen_links = set()

    print("[REUTERS] starting page-scrape candidate search")

    for page_url in REUTERS_SOURCE_PAGES:
        print(f"[REUTERS] source page: {page_url}")

        try:
            r = session.get(page_url, timeout=15)
            soup = BeautifulSoup(r.text, "lxml")
        except Exception as e:
            print(f"[REUTERS] page fetch failed: {e}")
            continue

        links = soup.find_all("a", href=True)

        for a in links:
            href = a["href"].strip()
            title = clean_text(a.get_text(" ", strip=True))

            if not href:
                continue

            if href.startswith("/"):
                href = "https://www.reuters.com" + href

            if not href.startswith("https://www.reuters.com/"):
                continue

            if href in seen_links:
                continue
            seen_links.add(href)

            if not title or len(title) < 20:
                continue

            # Only real article-like Reuters pages
            if href.count("/") < 5:
                continue

            # Skip obvious non-article media types
            if any(x in href for x in ["/podcasts/", "/video/", "/pictures/", "/graphics/", "/live/"]):
                continue

            if _looks_like_reuters_live_item(title, href):
                print(f"[REUTERS SKIP] live | {title}")
                continue

            # Try to find nearby summary + time from surrounding block
            summary = ""
            published_dt = None

            container = a.find_parent(["article", "div", "section"])
            if container:
                container_texts = [clean_text(x.get_text(" ", strip=True)) for x in container.find_all(["p", "span"], limit=8)]
                container_texts = [t for t in container_texts if t]

                for t in container_texts:
                    if not published_dt:
                        published_dt = _parse_reuters_relative_time(t)

                    if (
                        len(t) > 40
                        and t != title
                        and "reuters" not in t.lower()
                        and "includes video" not in t.lower()
                    ):
                        summary = t
                        break

            if not published_dt:
                published_dt = _parse_reuters_url_date(href)

            if not published_dt:
                continue

            age = now_utc - published_dt
            if age > timedelta(hours=REUTERS_BACKFILL_MAX_AGE_HOURS):
                print(f"[REUTERS SKIP] too old ({age}) | {title}")
                continue

            if not _is_reuters_relevant_text(title, summary, href):
                print(f"[REUTERS SKIP] not relevant | {title}")
                continue

            if _reuters_dupish(title, href, existing_top_developments + candidates):
                print(f"[REUTERS SKIP] dupish | {title}")
                continue

            print(f"[REUTERS KEEP] {title}")
            candidates.append({
                "source": "Reuters",
                "title": title,
                "link": href,
                "summary": summary,
                "published_at": published_dt.isoformat(),
                "_dt": published_dt,
            })

    print(f"[REUTERS] surviving candidates: {len(candidates)}")

    if not candidates:
        return []

    candidates.sort(key=lambda x: x["_dt"], reverse=True)
    selected = candidates[:limit]

    for item in selected:
        item.pop("_dt", None)

    print(f"[REUTERS] selected: {[item['title'] for item in selected]}")
    return selected

def get_reuters_backfill_candidate(existing_top_developments):
    candidates = get_reuters_backfill_candidates(existing_top_developments, limit=1)
    return candidates[0] if candidates else None

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

        published_at = None

        m = re.search(r"/(\d{4})-(\d{2})-(\d{2})/", url)
        if m:
            y, mth, d = m.groups()
            try:
                dt = datetime.strptime(f"{y}-{mth}-{d}", "%Y-%m-%d").replace(tzinfo=timezone.utc)
                published_at = dt.isoformat().replace("+00:00", "Z")
            except:
                published_at = None

        results.append({
            "source": "Haaretz",
            "title": title,
            "summary": url,
            "link": url,
            "published_at": published_at
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
            published_at = None

            dt_struct = e.get("published_parsed") or e.get("updated_parsed")
            if dt_struct:
                try:
                    dt = datetime(*dt_struct[:6], tzinfo=timezone.utc)
                    published_at = dt.isoformat().replace("+00:00", "Z")
                except:
                    published_at = None
            else:
                raw_date = e.get("published", "") or e.get("updated", "")
                if raw_date:
                    try:
                        dt = parsedate_to_datetime(raw_date)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        else:
                            dt = dt.astimezone(timezone.utc)
                        published_at = dt.isoformat().replace("+00:00", "Z")
                    except:
                        published_at = None

            items.append({
                "source": name,
                "title": e.get("title", ""),
                "summary": truncate(clean_html(e.get("summary", ""))),
                "link": e.get("link", ""),
                "published_at": published_at
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

    # 🔥 MEE crawler (replaces RSS)
    mee_articles = [a for a in crawl_middle_east_eye() if is_relevant(a)]
    items.extend(mee_articles)

    # 🔥 Al Jazeera Middle East crawler (replaces broad all-site RSS)
    al_jazeera_articles = [a for a in crawl_al_jazeera_middle_east() if is_relevant(a)]
    items.extend(al_jazeera_articles)

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

def deal_top_developments_by_source(candidates, limit=TOP_N):
    by_source = defaultdict(list)
    seen_links = set()

    for a in candidates:
        link = a.get("link")
        if link in seen_links:
            continue

        if not is_top_development_candidate(a):
            continue

        if is_podcast(a):
            continue

        if a.get("source") in ["Jadaliyya", "War on the Rocks", "Responsible Statecraft"]:
            continue

        seen_links.add(link)
        by_source[a["source"]].append(a)

    for source in by_source:
        by_source[source].sort(key=lambda x: x.get("importance", 0), reverse=True)

    source_rank = {
        source: rank
        for rank, source in enumerate(TOP_DEVELOPMENTS_SOURCE_ORDER)
    }

    source_order = sorted(
        by_source,
        key=lambda source: (
            source_rank.get(source, len(TOP_DEVELOPMENTS_SOURCE_ORDER)),
            -by_source[source][0].get("importance", 0)
        )
    )

    selected = []
    counts = defaultdict(int)

    while len(selected) < limit:
        added_this_round = False

        for source in source_order:
            if len(selected) >= limit:
                break

            max_for_source = MAX_PER_SOURCE.get(source, MAX_PER_SOURCE["default"])
            if counts[source] >= max_for_source:
                continue

            source_items = by_source[source]
            if counts[source] >= len(source_items):
                continue

            selected.append(source_items[counts[source]])
            counts[source] += 1
            added_this_round = True

        if not added_this_round:
            break

    return selected[:limit]

# ---------------------------
# BUILD
# ---------------------------

def build():
    raw, amwaj_articles = fetch_all()

    target_phrase = "why iran is not venezuela"

    print("\nSOURCE COUNTS:", Counter([a["source"] for a in raw]))
    for a in raw:
        if target_phrase in a.get("title", "").lower():
            debug_article("RAW", a)

    enriched = [
        summarize(a)
        for a in raw
        if not is_low_signal(a) and is_valid_article(a)
    ]

    for a in enriched:
        if target_phrase in a.get("title", "").lower():
            debug_article("ENRICHED", a)

    enriched.sort(key=lambda x: x["importance"], reverse=True)

    clusters = cluster_articles(enriched)
    deduped = select_representatives(clusters)

    for a in deduped:
        if target_phrase in a.get("title", "").lower():
            debug_article("DEDUPED", a)

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
            and is_top_development_candidate(a)
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
            and a["source"] != "Middle East Eye"
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
                or (
                    a.get("source") == "Middle East Eye"
                    and any(w in a.get("title", "").lower() for w in [
                        "why", "how", "analysis", "opinion", "what", "inside"
                    ])
                )
            )
            and not (
                a.get("source") == "War on the Rocks"
                and not is_geopolitically_relevant(a)
            )
        )
    ]



    for a in deep_candidates:
        if target_phrase in a.get("title", "").lower():
            debug_article("DEEP_CANDIDATE_BEFORE_SORT", a)            

    # 🔥 Prefer analytical sources
    deep_candidates = sorted(
        deep_candidates,
        key=lambda x: (
            0 if x["source"] in ["Jadaliyya", "Guardian", "Middle East Eye"] else
            1 if x["source"] == "Carnegie ME" else
            2 if x["source"] in ["War on the Rocks", "Responsible Statecraft"] else
            3,
            -x["importance"]
        )
    )

    # 🔥 CRITICAL: positional slice (restores old behavior)
    deep_slice = deep_candidates[:32]

    for a in deep_slice:
        if target_phrase in a.get("title", "").lower():
            debug_article("DEEP_SLICE", a)

    # Balance sources
    deep = balance_section(deep_slice, DEEP_N)

    for a in deep:
        if target_phrase in a.get("title", "").lower():
            debug_article("FINAL_DEEP", a)

    # 🔥 Ensure Amwaj Deep Dives are included
    amwaj_deep = [a for a in deduped if is_amwaj_deep_dive(a)]

    for a in reversed(amwaj_deep):
        if a not in deep:
            deep.insert(0, a)

    latest_amwaj_deep_dive = get_latest_amwaj_deep_dive()
    if latest_amwaj_deep_dive:
        latest_amwaj_deep_dive = summarize(latest_amwaj_deep_dive)

        if not any(a.get("link") == latest_amwaj_deep_dive.get("link") for a in deep):
            deep.insert(0, latest_amwaj_deep_dive)

        if len(deep) > DEEP_N:
            protected_link = latest_amwaj_deep_dive.get("link")
            for i in range(len(deep) - 1, -1, -1):
                if deep[i].get("link") != protected_link:
                    deep.pop(i)
                    break

    top_backfill_candidates = [
        a for a in deduped
        if (
            a not in events
            and is_top_development_candidate(a)
            and not is_amwaj_sitrep(a)
            and not is_amwaj_deep_dive(a)
            and not is_podcast(a)
            and a.get("source") not in ["Jadaliyya", "War on the Rocks", "Responsible Statecraft"]
        )
    ]

    top_development_deck = events + top_backfill_candidates
    events = deal_top_developments_by_source(top_development_deck)

    regional = [a for a in regional if a not in events]

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
    if latest_sitrep and is_top_development_candidate(latest_sitrep):

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

    cluster_text = "\n\n".join([
        "\n".join([
            (
                f"[{a.get('published_at') or 'UNKNOWN_TIME'}] "
                f"{a.get('source', 'Unknown Source')} — "
                f"{a.get('title', '')}\n"
                f"Summary: {a.get('ai_summary') or a.get('title', '')}"
            )
            for a in c[:2]
        ])
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
                    Write a concise 3–4 sentence narrative intelligence briefing that reads as a single coherent paragraph. Keep sentences controlled in length and avoid overloading any single sentence.

                    STYLE:
                    - Write in a smooth, natural narrative flow
                    - Avoid filler transitions (e.g., "Meanwhile", "At the same time")
                    - Use precise language
                    - Be specific about actors, actions, and locations
                    - Avoid generic or newsy phrasing
                    - Avoid attributional phrasing (e.g., "reports that", "according to"); write with direct analytical voice

                    TEMPORAL ANALYSIS:
                    - Use the timestamps to understand sequence and timing of developments
                    - Identify where events precede, trigger, or respond to one another
                    - Distinguish between ongoing baseline conditions and recent shifts
                    - Give more analytical weight to the most recent and consequential developments
                    - Do NOT assume the list order reflects chronology; rely on timestamps
                    - If timing is unclear, do not invent sequence — write cautiously

                    GOAL:
                    - Synthesize developments into a coherent regional picture
                    - Reflect political dynamics alongside societal and human impact
                    - Show how events are connected, not just occurring
                    - Surface causal relationships where timing supports them
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
