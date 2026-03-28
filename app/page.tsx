"use client";

import { useEffect, useState } from "react";

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
  "Al Monitor": "/logos/AlMonitor.jpeg"
};

function Card({ item, variant }: { item: Item; variant?: string }) {
  
  const isRegional = variant === "Regional Perspectives";
  
  return (
    <a
      href={item.link}
      target="_blank"
      className={`block rounded-lg border p-4 transition ${
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
       className={`text-sm mb-2 leading-relaxed ${
  isRegional
    ? "text-neutral-200"
    : "text-neutral-300"
}`} 
>
          {item.ai_summary}
        </div>
      )}

      {item.why && (
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

export default function Page() {
  const [data, setData] = useState<Briefing | null>(null);

  useEffect(() => {
    fetch("/briefing.json")
      .then((res) => res.json())
      .then(setData)
      .catch(console.error);
  }, []);

  if (!data) return null;

  return (
    <div className="bg-black text-white min-h-screen px-6 py-8">
      <div className="max-w-6xl mx-auto">

        <div className="mb-10">
          <div className="text-sm text-neutral-400 tracking-wide mb-2">
            A daily synthesis of reporting and analysis from across the region
          </div>

          <h1 className="text-2xl font-bold mb-2">
            Middle East & Iran Today
          </h1>

          <div className="text-sm text-neutral-400">
            Generated: {data.generated_at}
          </div>
        </div>

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
