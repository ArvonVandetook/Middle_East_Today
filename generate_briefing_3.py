import json
import os
import time
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
]

MAX_PER_SOURCE = {
    "Amwaj": 2,
    "default": 3
}

TOP_N = 8
REGIONAL_N = 8
DEEP_N = 10

AMWAJ_SEED_URL = "https://amwaj.media/en/media-monitor/tehran-vows-regional-escalation-after-trump-threatens-iranian-power-grid"
AMWAJ_MAX_ARTICLES = 20
AMWAJ_MAX_DEPTH = 2

# ---------------------------
# CLEAN
# ---------------------------

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<.*?>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def truncate(text, length=240):
    return text[:length].rsplit(" ", 1)[0] + "..." if len(text) > length else text

def clean_text(text):
    return " ".join(text.split()) if text else ""

def clean_title(t):
    t = re.sub(r":.*", "", t)
    return t.strip().capitalize() if t else t

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
    return any(k in text for k in KEY_TERMS)

# ---------------------------
# LOW SIGNAL
# ---------------------------

def is_low_signal(article):
    text = article["title"].lower()
    bad = ["what you need","latest","live","explainer","analysis:","how","what is","why"]
    return any(b in text for b in bad)

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
# SUMMARY
# ---------------------------

def summarize(article):

    if not client:
        article["ai_summary"] = article["summary"]
        article["importance"] = 50
        article["why"] = ""
    else:
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

            raw = r.choices[0].message.content
            parsed = json.loads(re.search(r"\{.*\}", raw, re.DOTALL).group())

            article["ai_summary"] = parsed.get("summary","")
            article["importance"] = parsed.get("importance",50)
            article["why"] = parsed.get("why","")

        except:
            article["importance"] = 50
            article["ai_summary"] = article["summary"]

    # 🔥 LIGHT BOOST
    if article["source"] == "Amwaj":
        article["importance"] += 6

    return article

# ---------------------------
# CLUSTERING
# ---------------------------

def extract_keywords(text):
    return set(re.findall(r"\b[a-z]{4,}\b", text.lower()))

def cluster_articles(articles):
    clusters, used = [], set()

    for i,a in enumerate(articles):
        if i in used:
            continue

        base = extract_keywords(a["title"])
        cluster = [a]
        used.add(i)

        for j,b in enumerate(articles[i+1:], i+1):
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
# AMWAJ CRAWLER (FIXED)
# ---------------------------

def extract_links(html, base):
    soup = BeautifulSoup(html, "lxml")
    links = set()

    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"])
        if "/en/" in url:
            links.add(url)

    return list(links)

def crawl_amwaj():
    visited = set()
    queue = [(AMWAJ_SEED_URL, 0)]
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while queue and len(results) < AMWAJ_MAX_ARTICLES:
            url, depth = queue.pop(0)

            if url in visited:
                continue
            visited.add(url)

            print(f"[AMWAJ] visiting depth {depth}: {url}")

            try:
                page.goto(url, timeout=30000)
                page.wait_for_timeout(2500)

                html = page.content()
                soup = BeautifulSoup(html, "lxml")

                title_tag = soup.find("h1")
                paragraphs = soup.select("p")

                if title_tag and paragraphs:
                    text = " ".join(p.get_text() for p in paragraphs[:5])

                    results.append({
                        "source": "Amwaj",
                        "title": clean_text(title_tag.get_text()),
                        "summary": truncate(clean_html(text)),
                        "link": url
                    })

                links = extract_links(html, url)
                print(f"[AMWAJ] found {len(links)} links")

                for l in links:
                    if l not in visited:
                        queue.append((l, depth + 1))

            except Exception as e:
                print("[AMWAJ ERROR]", e)

        browser.close()

    print(f"[AMWAJ] crawled: {len(results)}")
    return results

# ---------------------------
# FETCH
# ---------------------------

def fetch_rss(name, url):
    items = []

    try:
        r = session.get(url, timeout=10)
        feed = feedparser.parse(r.content)

        for e in feed.entries[:15]:
            summary = clean_html(e.get("summary","") or e.get("description",""))

            items.append({
                "source": name,
                "title": e.get("title",""),
                "summary": truncate(summary),
                "link": e.get("link","")
            })

    except Exception as e:
        print(f"RSS ERROR ({name}):", e)

    return items

def fetch_all():
    items = []

    for name, url in RSS_SOURCES:
        for a in fetch_rss(name, url):
            if is_relevant(a):
                items.append(a)

    amwaj = crawl_amwaj()
    print(f"[AMWAJ] before filter: {len(amwaj)}")

    amwaj = [a for a in amwaj if is_relevant(a)]
    print(f"[AMWAJ] after relevance: {len(amwaj)}")

    amwaj = [a for a in amwaj if not is_low_signal(a)]
    print(f"[AMWAJ] after low-signal: {len(amwaj)}")

    items.extend(amwaj)

    return items

# ---------------------------
# BALANCE
# ---------------------------

def balance_section(articles, limit):
    selected = []
    counts = defaultdict(int)

    for a in articles:
        src = a["source"]
        if counts[src] >= MAX_PER_SOURCE.get(src, 3):
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

    raw = fetch_all()

    print("\nSOURCE COUNTS:", Counter([a["source"] for a in raw]))

    enriched = [summarize(a) for a in raw]
    enriched = [a for a in enriched if not is_low_signal(a)]
    enriched.sort(key=lambda x: x["importance"], reverse=True)

    amwaj_scores = [a["importance"] for a in enriched if a["source"] == "Amwaj"]
    if amwaj_scores:
        print(f"[AMWAJ] avg importance: {sum(amwaj_scores)/len(amwaj_scores):.1f}")

    clusters = cluster_articles(enriched)
    deduped = select_representatives(clusters)

    events = [a for a in deduped if classify_event(a)]
    regional = [a for a in deduped if not classify_event(a)]

    events = balance_section(events, TOP_N)
    regional = balance_section(regional, REGIONAL_N)
    deep = balance_section(deduped[6:16], DEEP_N)

    final_amwaj = sum(1 for a in events + regional + deep if a["source"] == "Amwaj")
    print(f"[AMWAJ] final selected: {final_amwaj}")

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "top_story": generate_top_story(events, clusters),
        "top_developments": events,
        "regional_analysis": regional,
        "deep_analysis": deep
    }

# ---------------------------
# TOP STORY
# ---------------------------

def generate_top_story(events, clusters):

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
                {"role": "system", "content": "You are a geopolitical analyst."},
                {"role": "user", "content": f"Write a 2-3 sentence briefing:\n{cluster_text}"}
            ]
        )

        return r.choices[0].message.content.strip()

    except Exception:
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
