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
    bad = ["what you need","latest","live","explainer","analysis:","how","what is","why"]
    return any(b in article["title"].lower() for b in bad)

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
        article["importance"] -= 4

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
    queue=[(AMWAJ_SEED_URL,0)]
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
                    results.append({
                        "source":"Amwaj",
                        "title":clean_text(title.get_text()),
                        "summary":truncate(" ".join(p.get_text() for p in ps[:5])),
                        "link":url
                    })

                for l in extract_links(page.content(),url):
                    if l not in visited:
                        queue.append((l,1))

            except:
                continue

    print(f"[AMWAJ] crawled: {len(results)}")
    return results

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

    items.extend([a for a in crawl_amwaj() if is_relevant(a)])
    items.extend([a for a in crawl_jadaliyya() if is_relevant(a)])

    return items

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
    raw=fetch_all()

    print("\nSOURCE COUNTS:",Counter([a["source"] for a in raw]))

    enriched=[summarize(a) for a in raw if not is_low_signal(a)]
    enriched.sort(key=lambda x:x["importance"],reverse=True)

    clusters=cluster_articles(enriched)
    deduped=select_representatives(clusters)

    events=[a for a in deduped if classify_event(a)]
    regional=[a for a in deduped if not classify_event(a)]

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
    deep=balance_section(deduped[6:16],DEEP_N)
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
