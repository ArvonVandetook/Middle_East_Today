import { NextRequest } from "next/server";

export async function GET(req: NextRequest) {
  const url = req.nextUrl.searchParams.get("url");

  if (!url) {
    return new Response("Missing url", { status: 400 });
  }

  try {
    const res = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0"
      }
    });

    const text = await res.text();

    return new Response(text, {
      headers: {
        "Content-Type": "text/xml"
      }
    });

  } catch (e) {
    return new Response("Failed to fetch RSS", { status: 500 });
  }
}
