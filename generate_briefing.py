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
    ("Carnegie ME", "https://carnegie-mec.org/rss"),
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
DEEP_N = 10

AMWAJ_SEED_URL = "https://amwaj.media/en/media-monitor/tehran-vows-regional-escalation-after-trump-threatens-iranian-power-grid"
AMWAJ_MAX_ARTICLES = 20

JADALIYYA_SEED_URL = "https://www.jadaliyya.com/"
JAD_MAX_ARTICLES = 15

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
# RELEVANCE
# ---------------------------

KEY_TERMS = [
    "iran","israel","gaza","hezbollah","hamas","tehran","gulf",
    "saudi","uae","yemen","iraq","syria","lebanon","hormuz",
    "middle east","missile","drone","energy","oil"
]

def is_relevant(article):
    text = (article["title"] + " " + article.get("summary","")).lower()

    # Must include at least one core regional anchor
    core = [
        "iran","israel","gaza","hezbollah","hamas","tehran",
        "saudi","uae","yemen","iraq","syria","lebanon","hormuz",
        "middle east"
    ]

    if not any(k in text for k in core):
        return False

    # Exclude obvious non-region geopolitical topics
    excluded = ["taiwan", "south china sea", "ukraine", "korea"]

    if any(e in text for e in excluded):
        return False

    return True

# ---------------------------
# LOW SIGNAL
# ---------------------------

def is_low_signal(article):
    bad = ["what you need","latest","live","explainer","analysis:","how","what is","why"]
    return any(b in article["title"].lower() for b in bad)

def is_valid_article(article):
    title = article.get("title", "").lower()
    summary = article.get("summary", "").lower()
    url = article.get("link", "").lower()
    source = article.get("source", "")

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
    if len(summary.strip()) < 60:
        return False

    # 🔥 Source-specific rule (high impact)
    if source == "Amwaj":
        if "cookie" in url or "consent" in url:
            return False

    return True

# ---------------------------
# EVENT DETECTION
# ---------------------------

def classify_event(article):
    t = article["title"].lower()
    strong = ["strike","attack","missile","killed","explosion","drone","clashes"]
    weak = ["says","urges","talks","claims","discusses"]

    if any(w in t for w in weak):
        return False
    return any(s in t for s in strong)

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
                    "role":"user",
                    "content":f"""
Return JSON:
{{"summary":"","importance":0-100,"why":""}}

TITLE: {article['title']}
TEXT: {article['summary']}
"""
                }]
            )
            parsed = json.loads(re.search(r"\{.*\}", r.choices[0].message.content, re.DOTALL).group())
            article["ai_summary"] = parsed.get("summary","")
            article["importance"] = parsed.get("importance",50)
            article["why"] = parsed.get("why","")
        except:
            article["importance"] = 50
            article["ai_summary"] = article["summary"]
    else:
        article["importance"] = 50
        article["ai_summary"] = article["summary"]

    src = article["source"]

    # 🔥 Editorial weighting
    if src == "Amwaj":
        article["importance"] += 6
    elif src == "Jadaliyya":
        article["importance"] += 5
    elif src in ["Middle East Eye", "Al Monitor"]:
        article["importance"] += 3
    elif src == "Al Jazeera":
        article["importance"] += 1
    elif src in ["War on the Rocks", "Responsible Statecraft"]:
        article["importance"] -= 2

    return article

# ---------------------------
# CLUSTERING
# ---------------------------

def extract_keywords(text):
    return set(re.findall(r"\b[a-z]{4,}\b", text.lower()))

def cluster_articles(articles):
    clusters, used = [], set()

    for i,a in enumerate(articles):
        if i in used: continue

        base = extract_keywords(a["title"])
        cluster = [a]
        used.add(i)

        for j,b in enumerate(articles[i+1:], i+1):
            if j in used: continue
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
    return [urljoin(base,a["href"]) for a in soup.find_all("a", href=True) if "/en/" in a["href"]]

# 🔒 LOCKED FUNCTION — ingestion logic (fragile)

def crawl_amwaj():
    visited=set()
    queue = [
        ("https://amwaj.media/en/region/iran", 0),
        (AMWAJ_SEED_URL, 0)
    ]
    results=[]

    with sync_playwright() as p:
        page=p.chromium.launch(headless=True).new_page()

        while queue and len(results)<AMWAJ_MAX_ARTICLES:
            url,_=queue.pop(0)
            if url in visited: continue
            visited.add(url)

            try:
                page.goto(url)
                page.wait_for_timeout(2000)
                soup=BeautifulSoup(page.content(),"lxml")

                title=soup.find("h1")
                ps=soup.select("p")

                if title and ps:

                    if not any(x in url for x in ["/article/", "/media-monitor/"]):
                        continue

                    # --- extract date from page text ---
                    date_text = ""

                    for el in soup.find_all(text=True):
                        txt = clean_text(el)
                        if re.match(r"[A-Z][a-z]{2}\.\s\d{1,2},\s\d{4}", txt):
                            date_text = txt
                            break

                    results.append({
                        "source":"Amwaj",
                        "title":clean_text(title.get_text()),
                        "summary":truncate(" ".join(p.get_text() for p in ps[:5])),
                        "link":url,
                        "date": date_text   # ✅ NEW
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
    results=[]

    with sync_playwright() as p:
        page=p.chromium.launch(headless=True).new_page()
        page.goto(JADALIYYA_SEED_URL)
        page.wait_for_timeout(3000)

        soup=BeautifulSoup(page.content(),"lxml")

        links=list(set([
            urljoin(JADALIYYA_SEED_URL,a["href"])
            for a in soup.find_all("a",href=True)
            if "/Details/" in a["href"]
        ]))[:JAD_MAX_ARTICLES]

    print(f"[JAD] links: {len(links)}")

    for url in links:
        try:
            s=BeautifulSoup(session.get(url).text,"lxml")
            title=s.find("h1")
            ps=s.select("p")

            if title and ps:
                results.append({
                    "source":"Jadaliyya",
                    "title":clean_text(title.get_text()),
                    "summary":truncate(" ".join(p.get_text() for p in ps[:6])),
                    "link":url
                })
        except:
            continue

    print(f"[JAD] crawled: {len(results)}")
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

    for url in links[:10]:   # limit for safety    
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

def fetch_rss(name,url):
    items=[]
    try:
        feed=feedparser.parse(session.get(url).content)
        for e in feed.entries[:15]:
            items.append({
                "source":name,
                "title":e.get("title",""),
                "summary":truncate(clean_html(e.get("summary",""))),
                "link":e.get("link","")
            })
    except:
        pass
    return items

def fetch_all():
    items=[]

    for n,u in RSS_SOURCES:
        items.extend([a for a in fetch_rss(n,u) if is_relevant(a)])

    items.extend([a for a in crawl_haaretz() if is_relevant(a)])

    # 🔥 Capture Amwaj separately
    amwaj_articles = [a for a in crawl_amwaj() if is_relevant(a)]

    # 🔥 Capture Jadaliyya separately (optional but clean)
    jad_articles = [a for a in crawl_jadaliyya() if is_relevant(a)]

    # Add to main pool
    items.extend(amwaj_articles)
    items.extend(jad_articles)

    return items, amwaj_articles

# ---------------------------
# BALANCE
# ---------------------------

def balance_section(articles,limit):
    selected=[]
    counts=defaultdict(int)

    for a in articles:
        src=a["source"]
        if counts[src]>=MAX_PER_SOURCE.get(src,2): continue
        selected.append(a)
        counts[src]+=1
        if len(selected)>=limit: break

    return selected

# ---------------------------
# BUILD
# ---------------------------

def build():
    raw, amwaj_articles = fetch_all()

    print("\nSOURCE COUNTS:",Counter([a["source"] for a in raw]))

    enriched = [
        summarize(a)
        for a in raw
        if not is_low_signal(a) and is_valid_article(a)
    ]
    enriched.sort(key=lambda x:x["importance"],reverse=True)

    clusters=cluster_articles(enriched)
    deduped=select_representatives(clusters)

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
        if not classify_event(a)
        and not is_amwaj_sitrep(a)
        and not is_amwaj_deep_dive(a)
    ]


    # ---------------------------
    # DEEP ANALYSIS (RESTORED)
    # ---------------------------

    deep_candidates = [
        a for a in deduped
        if (
            not classify_event(a)
            and not is_amwaj_sitrep(a)
        )
    ]

    # 🔥 Maintain ranking
    deep_candidates = sorted(
        deep_candidates,
        key=lambda x: x["importance"],
        reverse=True
    )

    # 🔥 CRITICAL: positional slice (restores old behavior)
    deep_slice = deep_candidates[6:20]

#    Balance sources
    deep = balance_section(deep_slice, DEEP_N)

    # 🔥 Ensure Amwaj Deep Dives are included
    amwaj_deep = [a for a in deduped if is_amwaj_deep_dive(a)]

    for a in reversed(amwaj_deep):
        if a not in deep:
            deep.insert(0, a)

    # 🔥 Backfill
    if len(events)<TOP_N:
        for a in sorted(regional,key=lambda x:x["importance"],reverse=True):
            if a not in events:
                events.append(a)
            if len(events)>=TOP_N: break

    regional=[a for a in regional if a not in events]
    # --- Ensure Haaretz representation ---
   

    events=balance_section(events,TOP_N)
    regional=balance_section(regional,REGIONAL_N)



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

    # 🔥 FORCE Sitrep as anchor (deterministic placement)

    if latest_sitrep:

        # Remove ANY sitrep-like items
        events = [
            a for a in events
            if "sitrep" not in a["title"].lower()
        ]

        # Ensure we don't exceed TOP_N
        if len(events) >= TOP_N:
            events = events[:TOP_N - 1]

        # Insert Sitrep at top
        events.insert(0, latest_sitrep)


    print("FINAL TOP SOURCES:", [a["source"] for a in events])
    print("FINAL REGIONAL SOURCES:", [a["source"] for a in regional])

    return {
        "generated_at": datetime.utcnow().isoformat()+"Z",
        "top_story": generate_top_story(events,clusters),
        "top_developments": events,
        "regional_analysis": regional,
        "deep_analysis": deep
    }


# ---------------------------
# TOP STORY
# ---------------------------

# 🔒 LOCKED FUNCTION — DO NOT MODIFY
# Controls Top Story narrative quality
def generate_top_story(events, clusters):

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
                        "You are a geopolitical intelligence analyst. "
                        "Write clear, fluent, and natural intelligence briefings that synthesize multiple developments."
                    )
                },
                {
                    "role": "user",
                    "content": f"""
Write a concise 2–3 sentence intelligence briefing.

STYLE:
- Write in a smooth, natural narrative flow
- Avoid filler transitions (e.g., "Meanwhile", "At the same time")
- Use strong, direct verbs
- Be specific (actors, actions, locations)
- Avoid generic phrasing

GOAL:
- Connect developments into a coherent picture
- Show how the situation is evolving
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
    os.makedirs("public",exist_ok=True)
    with open("public/briefing.json","w") as f:
        json.dump(data,f,indent=2)

# ---------------------------
# MAIN
# ---------------------------

if __name__=="__main__":
    briefing=build()
    save(briefing)
    print("Saved briefing.json")
