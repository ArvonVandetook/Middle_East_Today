"use client";

import { useEffect, useState } from "react";

const SHOW_WHY = false;

type Item = {
  source: string;
  title: string;
  link: string;
  ai_summary: string;
  importance: number;
  why?: string;
};

type Briefing = {
  generated_at: string;
  top_story?: string;
  top_developments: Item[];
  regional_analysis: Item[];
  deep_analysis: Item[];
};

const SOURCE_LOGOS: Record<string, string> = {
  "Middle East Eye": "/logos/ME.png",
  "Guardian": "/logos/Guardian.jpeg",
  "Al Jazeera": "/logos/aljazeera.jpeg",
  "Haaretz": "/logos/Haaretz.png",
  "Amwaj": "/logos/Amwaj.png",
  "Jadaliyya": "/logos/jadaliyya.png",
  "Al Monitor": "/logos/AlMonitor.jpeg",
  "Responsible Statecraft": "/logos/rs.jpg",
  "War on the Rocks": "/logos/WOTR.jpg",
  "Carnegie ME": "/logos/Carnegie.jpg",
  "Drop Site News": "/logos/dropsite.jpeg"
};

function Card({ item, variant }: { item: Item; variant?: string }) {
  
  const isRegional = variant === "Regional Perspectives";
  
  return (
    <a
      href={item.link}
      target="_blank"
      className={`block rounded-lg border p-3 transition ${
  isRegional
    ? "border-neutral-800/60 hover:border-neutral-700 bg-neutral-900/20"
    : "border-neutral-800 hover:border-neutral-700"
}`}
    >
      <div className="flex items-center gap-2 text-xs text-neutral-300 mb-1">
  {SOURCE_LOGOS[item.source] && (
    <img
      src={SOURCE_LOGOS[item.source]}
      alt={item.source}
      className="w-8 h-8 object-contain"
    />
  )}
  <span>{item.source}</span>
</div>

      <div className="font-semibold text-[15px] text-neutral-100 mb-1 leading-snug">
        {item.title}
      </div>

      {item.ai_summary && (
        <div
       className={`text-sm mb-1 leading-snug ${
  isRegional
    ? "text-neutral-200"
    : "text-neutral-300"
}`} 
>
          {item.ai_summary}
        </div>
      )}

      {SHOW_WHY && item.why && (
        <div
  className={`text-sm leading-relaxed ${
    isRegional
      ? "text-yellow-400/80"
      : "text-yellow-400/100"
  }`}
>
          <span className="font-semibold">Why it matters: </span>
          {item.why}
        </div>
      )}
    </a>
  );
}

async function fetchLatestPost(rssUrl: string) {
  
  try {
    const res = await fetch(`/api/rss?url=${encodeURIComponent(rssUrl)}`);
    const text = await res.text();

    const parser = new DOMParser();
    const xml = parser.parseFromString(text, "text/xml");

    const item = xml.querySelector("item");

    if (!item) return null;

    const pubDate = item.querySelector("pubDate")?.textContent || "";

    const date = pubDate ? new Date(pubDate) : null;

    const timeAgo = date
      ? Math.floor((Date.now() - date.getTime()) / (1000 * 60 * 60 * 24))
      : null;

    return {
      title: item.querySelector("title")?.textContent || "",
      link: item.querySelector("link")?.textContent || "",
      daysAgo: timeAgo
    };

  } catch (e) {
    console.error("RSS fetch failed:", rssUrl);
    return null;
  }
}

//
// ✅ NEW: Analyst Voices config
//
const ANALYST_FEEDS = [
  { name: "Trita Parsi", rss: "https://tritaparsi.substack.com/feed" },
  { name: "Holly Dagres", rss: "https://www.theiranist.com/feed" },
  { name: "Ali Ansari", rss: "https://iranshahr.substack.com/feed" },
  { name: "Middle East Politics", rss: "https://mideastpolitics.substack.com/feed" },
  { name: "IranWire", rss: "https://iranwire.substack.com/feed" },
  { name: "James M. Dorsey", rss: "https://jamesmdorsey.substack.com/feed" },
  { name: "Fatima Abo Alasrar", rss: "https://www.ideologymachine.com/feed" },
  { name: "Greg Carlstrom", rss: "https://www.economist.com/middle-east-and-africa/rss.xml" },
 
];

function AnalystVoices() {
  const [posts, setPosts] = useState<Record<string, any>>({});

  useEffect(() => {
  ANALYST_FEEDS.forEach(async (a) => {
    const post = await fetchLatestPost(a.rss);

    setPosts((prev) => ({
      ...prev,
      [a.name]: post || null
    }));
  });
}, []);

  return (
    <div className="mb-14 border-t border-neutral-800 pt-8">
      <h2 className="text-lg font-semibold mb-4">Analyst Voices</h2>

      <div className="space-y-4">
        {ANALYST_FEEDS.map((a, i) => {
          const post = posts[a.name];

          return (
            <div key={i} className="text-sm text-neutral-300 border-b border-neutral-800/60 pb-2"
              >              
              <span className="font-semibold text-neutral-100">
                {a.name}
              </span>

              {post ? (
                <>
                  {" — "}
                  <a
                    href={post.link}
                    target="_blank"
                    className="text-yellow-300 hover:text-yellow-200 transition"                  
                    >
                    {post.title}
                    {post.daysAgo !== null && (
                      <span className="text-neutral-600 ml-1"> · {post.daysAgo}d ago</span>
                    )}
                  </a>
                </>
              ) : (
                <>
                  {" — "}
                  <span className="text-neutral-500">
                    (no recent posts)
                  </span>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function Page() {
  const [data, setData] = useState<Briefing | null>(null);

  useEffect(() => {
    fetch("/briefing.json")
      .then((res) => res.json())
      .then(setData)
      .catch(console.error);
  }, []);

  if (!data) return null;

  const date = new Date(data.generated_at);

  const pacificDate = date.toLocaleDateString("en-GB", {
    timeZone: "America/Los_Angeles",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });

  const pacificTime = date.toLocaleTimeString("en-GB", {
    timeZone: "America/Los_Angeles",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  const zuluTime = date.toISOString().slice(11, 16);

  return (    <div className="bg-black text-white min-h-screen px-6 py-8">
      <div className="max-w-6xl mx-auto">

        <div className="mb-10">
          <div className="text-sm text-neutral-400 tracking-wide mb-2">
            A daily synthesis of reporting and analysis from across the region
          </div>

          <h1 className="text-2xl font-bold mb-2">
            Middle East & Iran Today
          </h1>

          <div className="text-sm text-neutral-400">
            Generated: {pacificDate} · {pacificTime} PT / {zuluTime}Z
          </div>        </div>

        {data.top_story && (
          <div className="mb-14 border-b border-neutral-800 pb-8">
            <div className="text-lg font-semibold text-white mb-4">
              The Big Picture
            </div>
            <div className="text-lg md:text-lg leading-relaxed text-neutral-100 font-light mb-2">
              {data.top_story}
            </div>
          </div>
        )}

        <Section title="Top Developments" items={data.top_developments} />
        <Section title="Regional Perspectives" items={data.regional_analysis} />
        <Section title="Deeper Analysis" items={data.deep_analysis} />

        {/* ✅ NEW SECTION — completely isolated */}
        <AnalystVoices />

      </div>
    </div>
  );
}

function Section({ title, items }: { title: string; items: Item[] }) {
  if (!items || items.length === 0) return null;

  return (
    <div className="mb-12">
      <h2 className="text-lg font-semibold mb-4">{title}</h2>

      <div className="grid md:grid-cols-3 gap-4">
        {items.map((item, i) => (
          <Card key={i} item={item} variant={title}/>
        ))}
      </div>
    </div>
  );
}
