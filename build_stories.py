#!/usr/bin/env python3
"""
Both Sides Daily: daily story builder (free version).

1. Reads public RSS feeds from outlets grouped by political lean (no key needed).
2. Asks Google's Gemini API (free tier) to pick the day's top stories and write
   the bullets, summary, per-side takes, arguments, evidence and rebuttals.
3. Saves stories.json (archive) and stories.js (what both-sides.html loads).

Setup:   pip install feedparser requests
Key:     get a free key at aistudio.google.com, then set GEMINI_API_KEY
Check:   python build_stories.py --check     (tests every feed, no AI call)
Run:     python build_stories.py
Daily:   GitHub Actions (see update-stories.yml) or cron.
Optional: set GEMINI_MODEL to force a model name (see aistudio.google.com).
"""
import datetime as dt
import html
import json
import os
import re
import sys
import time

import feedparser
import requests

API_KEY = os.environ.get("GEMINI_API_KEY")
API = "https://generativelanguage.googleapis.com/v1beta"
HERE = os.path.dirname(os.path.abspath(__file__))
PER_FEED, PER_LEAN, MAX_AGE_HOURS = 8, 24, 36
UA = {"User-Agent": "Mozilla/5.0 (Both Sides Daily personal project)"}
TOPICS = ["Economy", "Immigration", "Healthcare", "Foreign Policy", "Technology",
          "Environment", "Courts & Law", "Elections", "Other"]

# Feed addresses change over time. Run with --check to see which ones work,
# then edit this list. Lean describes the outlet, based on commonly published
# ratings such as AllSides. It is a starting point, so adjust it to your judgment.
FEEDS = {
    "left": [
        ("NPR", "https://feeds.npr.org/1014/rss.xml"),
        ("The Guardian", "https://www.theguardian.com/us-news/us-politics/rss"),
        ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml"),
        ("The Washington Post", "https://feeds.washingtonpost.com/rss/politics"),
        ("HuffPost", "https://www.huffpost.com/section/politics/feed"),
    ],
    "center": [
        ("BBC News", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
        ("The Hill", "https://thehill.com/homenews/feed/"),
        ("Axios", "https://api.axios.com/feed/"),
        ("Christian Science Monitor", "https://rss.csmonitor.com/feeds/politics"),
    ],
    "right": [
        ("Fox News", "https://moxie.foxnews.com/google-publisher/politics.xml"),
        ("New York Post", "https://nypost.com/politics/feed/"),
        ("National Review", "https://www.nationalreview.com/feed/"),
        ("Washington Examiner", "https://www.washingtonexaminer.com/feed"),
        ("The Daily Caller", "https://dailycaller.com/feed/"),
        ("The Washington Times", "https://www.washingtontimes.com/rss/headlines/news/politics/"),
    ],
}


def clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def read_feed(url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def check_feeds():
    for lean, feeds in FEEDS.items():
        for name, url in feeds:
            try:
                n = len(read_feed(url))
                print(f"{'OK  ' if n else 'EMPTY'} [{lean}] {name}: {n} items")
            except Exception as e:  # noqa: BLE001
                print(f"FAIL [{lean}] {name}: {e}")


def fetch_articles():
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    items, seen = [], set()
    for lean, feeds in FEEDS.items():
        group = []
        for name, url in feeds:
            try:
                entries = read_feed(url)
            except Exception as e:  # noqa: BLE001
                print(f"Skipping {name}: {e}", file=sys.stderr)
                continue
            taken = 0
            for e in entries:
                link, title = e.get("link"), clean(e.get("title"))
                stamp = e.get("published_parsed") or e.get("updated_parsed")
                if not link or not title or link in seen:
                    continue
                if stamp and time.mktime(stamp) < cutoff - time.timezone:
                    continue
                seen.add(link)
                group.append({"lean": lean, "source": name, "title": title, "url": link,
                              "text": clean(e.get("summary") or e.get("description"))[:500]})
                taken += 1
                if taken >= PER_FEED:
                    break
        for a in group[:PER_LEAN]:
            a["id"] = f"a{len(items) + 1}"
            items.append(a)
    return items


SYSTEM = f"""You are an even-handed news analyst helping a student prepare for political debates.
You receive numbered articles (headline plus a short snippet) from left-leaning, centrist and right-leaning outlets.
Choose the 3 to 5 biggest political or world stories covered by several of them and return ONLY valid JSON
in this shape:
{{"stories":[{{
 "topic": one of {TOPICS},
 "headline": neutral, factual, no loaded words,
 "points": [3 to 4 short bullets of the key ideas/arguments],
 "summary": "3 to 5 sentences",
 "sides": {{
   "d": {{"take": "1 to 2 sentences", "article_ids": ["a3"]}},
   "r": {{"take": "...", "article_ids": ["a7"]}},
   "m": {{"take": "...", "article_ids": ["a9"]}} or null if no distinct middle position exists}},
 "deep": {{
   "background": "2 to 4 sentences of context",
   "agree": ["facts or points both sides accept"],
   "caseD": [{{"claim": "...", "why": "their reasoning", "evidence": ["2 items"], "rebuttal": "what Republicans say back"}}],
   "caseR": [{{"claim": "...", "why": "...", "evidence": ["2 items"], "rebuttal": "what Democrats say back"}}]}}}}]}}
Rules:
- "d" and "r" describe the positions of Democratic and Republican officials, candidates and voters as the articles
  report them. Do not treat an outlet's own opinion as a party's position.
- Use 2 arguments per side. Plain language. Same tone and effort for both sides. No editorializing.
- Cite only article ids from the input. Never invent URLs, quotes, numbers or studies.
- Evidence must come from the supplied article text. If the coverage gives none, write
  "Supporters did not cite specific evidence in the coverage reviewed."
- Where a claim is disputed, say it is disputed instead of picking a winner."""


def candidate_models():
    forced = os.environ.get("GEMINI_MODEL")
    if forced:
        return [forced]
    r = requests.get(f"{API}/models", headers={"x-goog-api-key": API_KEY},
                     params={"pageSize": 200}, timeout=30)
    r.raise_for_status()
    found = []
    for m in r.json().get("models", []):
        name = m["name"].split("/")[-1]
        ok = "generateContent" in m.get("supportedGenerationMethods", [])
        mt = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?", name)
        if ok and mt:
            found.append((float(mt.group(1)), not mt.group(2), name))
    found.sort(reverse=True)  # newest version first, regular Flash before Flash-Lite
    return [n for _, _, n in found[:3]]


def generate(model, listing):
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": listing}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.3, "maxOutputTokens": 16000}}
    for attempt in range(3):
        r = requests.post(f"{API}/models/{model}:generateContent",
                          headers={"x-goog-api-key": API_KEY}, json=body, timeout=180)
        if r.status_code in (429, 500, 503) and attempt < 2:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    parts = r.json()["candidates"][0]["content"]["parts"]
    raw = "".join(p.get("text", "") for p in parts).strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)["stories"]


def ask_ai(articles):
    listing = "\n".join(
        f"[{a['id']}] ({a['lean']}, {a['source']}) {a['title']} :: {a['text']}" for a in articles)
    for model in candidate_models():
        try:
            print(f"Using model: {model}")
            return generate(model, listing)
        except (requests.RequestException, KeyError, IndexError, ValueError) as e:
            print(f"{model} failed: {e}", file=sys.stderr)
    sys.exit("No model worked. Set GEMINI_MODEL to a free Flash model listed in Google AI Studio.")


def build(stories, articles, today):
    by_id = {a["id"]: a for a in articles}

    def sources(ids):
        return [[by_id[i]["source"], by_id[i]["url"]] for i in ids if i in by_id]

    out = []
    for n, s in enumerate(stories, 1):
        sides = {}
        for k in ("d", "r", "m"):
            side = (s.get("sides") or {}).get(k)
            src = sources(side.get("article_ids", [])) if side else []
            sides[k] = {"take": side["take"], "src": src} if side and src else None
        if not sides["d"] or not sides["r"]:
            continue  # need linked sources for both main sides
        slug = re.sub(r"[^a-z0-9]+", "-", s["headline"].lower()).strip("-")[:40]
        out.append({"id": f"{today}-{n}-{slug}", "date": today,
                    "topic": s.get("topic") if s.get("topic") in TOPICS else "Other",
                    "headline": s["headline"], "points": s["points"], "summary": s["summary"],
                    "sides": sides, "deep": s["deep"]})
    return out


def main():
    if "--check" in sys.argv:
        return check_feeds()
    if not API_KEY:
        sys.exit("Set GEMINI_API_KEY first (free key at aistudio.google.com).")
    today = dt.date.today().isoformat()
    articles = fetch_articles()
    if len({a["lean"] for a in articles}) < 3:
        sys.exit("Not enough articles from all three groups. Run with --check to see which feeds fail.")
    new = build(ask_ai(articles), articles, today)
    if not new:
        sys.exit("The AI returned no usable stories. Nothing was saved.")

    path = os.path.join(HERE, "stories.json")
    old = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = [s for s in json.load(f)["stories"] if s["date"] != today]
    data = {"updated": today, "stories": sorted(old + new, key=lambda s: s["date"], reverse=True)}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    with open(os.path.join(HERE, "stories.js"), "w", encoding="utf-8") as f:
        f.write("window.STORIES_DATA=" + json.dumps(data, ensure_ascii=False) + ";")
    print(f"Saved {len(new)} stories for {today} ({len(data['stories'])} total).")


if __name__ == "__main__":
    main()
