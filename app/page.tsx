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

function Card({ item }: { item: Item }) {
  return (
    <a
      href={item.link}
      target="_blank"
      className="block rounded-xl border border-neutral-800 p-4 hover:border-neutral-600 transition"
    >
      <div className="text-xs text-neutral-400 mb-1">{item.source}</div>

      <div className="font-semibold text-sm mb-2">{item.title}</div>

      {item.ai_summary && (
        <div className="text-sm text-neutral-300 mb-2">
          {item.ai_summary}
        </div>
      )}

      {item.why && (
        <div className="text-xs text-yellow-400 mt-2 leading-relaxed">
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
          <div className="text-xs tracking-widest text-neutral-500 mb-2">
            DAILY INTELLIGENCE BRIEF
          </div>

          <h1 className="text-2xl font-bold mb-2">
            Middle East / Iran Intelligence Dashboard
          </h1>

          <div className="text-xs text-neutral-500">
            Generated: {data.generated_at}
          </div>
        </div>

        {data.top_story && (
          <div className="mb-14 border-b border-neutral-800 pb-8">
            <div className="text-xs text-neutral-500 tracking-widest mb-4">
              TOP STORY
            </div>
            <div className="text-xl md:text-2xl leading-relaxed text-neutral-100 font-light max-w-4xl">
              {data.top_story}
            </div>
          </div>
        )}

        <Section title="Top Developments" items={data.top_developments} />
        <Section title="Regional Perspective" items={data.regional_analysis} />
        <Section title="Deep Analysis" items={data.deep_analysis} />

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
          <Card key={i} item={item} />
        ))}
      </div>
    </div>
  );
}
