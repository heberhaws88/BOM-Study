"""
Daily Book of Mormon Study Generator
-------------------------------------
Each run:
  1. Figures out today's reading (using state.json as a bookmark)
  2. Pulls the actual chapter text from churchofjesuschrist.org
  3. Asks Claude to write a 15-25 minute institute-style commentary
     (witty, Bytheway-ish, tied to life application + history/war stories)
  4. Converts that script to an MP3 with OpenAI TTS
  5. Publishes it to a podcast feed (GitHub Pages)
  6. Advances the bookmark in state.json (the GitHub Action commits this back)

Run with --dry-run to print the fetched chapter + generated script WITHOUT
calling TTS or touching the feed or advancing state. Good for testing/debugging.
"""

import os
import sys
import json
import time
import base64
import argparse
import requests
from bs4 import BeautifulSoup

STATE_FILE = "state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BoMStudyBot/1.0)"}

# ---------------------------------------------------------------------------
# 1. THE SYLLABUS: intro material, then every chapter of the Book of Mormon
#    in canonical order. (uri, display title)
# ---------------------------------------------------------------------------

INTRO_ITEMS = [
    ("bofm-title", "Title Page"),
    ("introduction", "Introduction"),
    ("three", "Testimony of Three Witnesses"),
    ("eight", "Testimony of Eight Witnesses"),
    ("js", "Testimony of the Prophet Joseph Smith"),
]

BOOKS = [
    ("1-ne", "1 Nephi", 22),
    ("2-ne", "2 Nephi", 33),
    ("jacob", "Jacob", 7),
    ("enos", "Enos", 1),
    ("jarom", "Jarom", 1),
    ("omni", "Omni", 1),
    ("w-of-m", "Words of Mormon", 1),
    ("mosiah", "Mosiah", 29),
    ("alma", "Alma", 63),
    ("hel", "Helaman", 16),
    ("3-ne", "3 Nephi", 30),
    ("4-ne", "4 Nephi", 1),
    ("morm", "Mormon", 9),
    ("ether", "Ether", 15),
    ("moro", "Moroni", 10),
]


def build_syllabus():
    syllabus = []
    intro_titles = [title for _, title in INTRO_ITEMS]
    syllabus.append(
        {
            "uris": [f"/scriptures/bofm/{uri}" for uri, _ in INTRO_ITEMS],
            "title": "Introduction to the Book of Mormon",
            "section_labels": intro_titles,
        }
    )
    for slug, name, num_chapters in BOOKS:
        for ch in range(1, num_chapters + 1):
            syllabus.append(
                {
                    "uri": f"/scriptures/bofm/{slug}/{ch}",
                    "title": f"{name} {ch}",
                }
            )
    return syllabus


# ---------------------------------------------------------------------------
# 2. FETCH CHAPTER TEXT
# ---------------------------------------------------------------------------

def fetch_chapter_text(uri: str) -> str:
    """Scrapes the verse text for a given scripture URI from
    churchofjesuschrist.org. Returns plain text with verse numbers."""
    url = f"https://www.churchofjesuschrist.org/study{uri}?lang=eng"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    verses = soup.select("p.verse, p[class*='verse']")
    if verses:
        lines = []
        for v in verses:
            # verse number is usually its own span at the start of the <p>
            num_tag = v.find("span")
            num = num_tag.get_text(strip=True) if num_tag else ""
            if num_tag:
                num_tag.extract()
            text = v.get_text(" ", strip=True)
            lines.append(f"{num} {text}".strip())
        return "\n".join(lines)

    # Fallback for non-verse pages (title page, introduction, testimonies):
    body = soup.select_one("div#study-content, div.body-block, main")
    if body:
        paragraphs = body.find_all("p")
        return "\n\n".join(p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True))

    raise RuntimeError(
        f"Couldn't find chapter text at {url}. The site's HTML structure "
        "may have changed — inspect the page and update the CSS selectors "
        "in fetch_chapter_text()."
    )


def fetch_combined_text(uris: list, section_labels: list) -> str:
    """Fetches several scripture pages and stitches them into one labeled block,
    used for the combined Introduction episode."""
    parts = []
    for uri, label in zip(uris, section_labels):
        text = fetch_chapter_text(uri)
        parts.append(f"=== {label} ===\n{text}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 3. GENERATE THE COMMENTARY SCRIPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You write daily Book of Mormon study scripts meant to be listened to as audio.
The voice is a blend of two real speakers, plus one outside framework — you are NOT writing a
dry academic commentary.

VOICE BLEND:
- John Bytheway: witty, conversational, self-deprecating, quick with a clever turn of phrase,
  makes you smile before he makes you think.
- Elder Patrick Kearon (Quorum of the Twelve Apostles): warm and deeply compassionate,
  narrative-driven — he often opens with a vivid human story and lets it breathe before drawing
  the doctrine out of it. Emotionally direct, tender toward people who are struggling, searching,
  or feel like outsiders. Not afraid of a quiet, sincere moment even inside an otherwise upbeat
  talk.
- Blend these naturally within a single episode rather than switching modes abruptly — humor and
  tenderness can sit right next to each other, the way they do in a good sacrament meeting talk.

LEADERSHIP AND ACCOUNTABILITY THREAD:
- The listener is a soldier who cares about leadership and personal accountability. The Book of
  Mormon is full of moments that embody hard-nosed leadership principles: leaders who own every
  outcome instead of blaming their people or their circumstances, leaders who check their ego,
  captains and prophets who believe fully in their mission and get others to believe in it too,
  people who cover each other rather than succeeding alone, moments of radical personal
  responsibility for failure, and the discipline that produces real freedom rather than
  restricting it.
- When a chapter shows one of these dynamics — Mormon taking responsibility for his record's
  faults, Captain Moroni's decisiveness, Helaman's stripling warriors trusting their training,
  Alma the Younger owning his own turnaround rather than explaining it away — name the leadership
  principle plainly and show how the text itself demonstrates it. Draw the insight straight out
  of the scripture; do not reference or attribute it to any modern author, book, or outside
  framework. It should feel like it was always there in the text, because it was.
- This should feel like a genuine "huh, that's exactly what real leadership looks like" insight,
  not a forced tie-in every single day. Some chapters (covenant, atonement, personal repentance,
  faith) may connect more to Kearon's compassionate register than to leadership at all, and
  that's fine — never manufacture a leadership angle where the text is really about something
  else.

THE THROUGHLINE — JOY:
- Underneath the humor, the history, and the leadership moments, the point of every single
  episode is the same: "Men are that they might have joy" (2 Nephi 2:25) — this book is
  ultimately about finding real, durable happiness, not just about doing more or trying harder.
  Every episode should circle back to that somewhere, even briefly.
- Don't let this collapse into forced positivity or a slogan. Ground it in the specific chapter:
  what does THIS chapter say about where joy actually comes from, what gets in its way, or what
  it costs? Ownership, discipline, and hard obedience are in service of joy, not a substitute
  for it — when you draw out a leadership moment, it's worth naming why the discipline matters:
  not for its own sake, but because it's what actually produces a life you can be happy in. Some
  days that throughline is a single closing sentence; other days the whole chapter is explicitly
  about joy or its opposite (bondage, misery, guilt) and it can run all the way through.

GENERAL RULES:
- Talk TO the listener like a trusted teacher would, not AT them. Use "you," ask rhetorical
  questions, use asides.
- Go through the chapter more or less in order, pulling out the verses/moments that matter most —
  you don't need to comment on every single verse.
- For each major point, connect it to real, concrete life application. Don't moralize in the
  abstract — give a specific, relatable scenario.
- Weave in ONE OR TWO outside stories per episode where they genuinely fit — history, literature,
  sports, or everyday life. War stories land especially well for him, and Revolutionary War
  stories are a particular favorite — reach for one when there's a real, non-forced connection
  (courage under fire, holding a line, unlikely leadership, sacrifice, brotherhood). Never force
  a war story into a chapter where it doesn't fit; variety is good, and a leadership moment drawn
  straight from the scripture text can stand in for the "outside story" slot on days when that
  fits better than history does.
- Close with a short, practical "take this into your day" thought — one or two sentences, not
  a bullet list.
- Target length: written for spoken delivery at roughly 150 words per minute, aiming for an
  18-22 minute episode (roughly 2700-3300 words). Do not pad — if the chapter is short and
  simple (like Enos or Jarom), a slightly shorter episode is fine.
- Write it as a script meant to be read aloud start to finish — no headers, no bullet points,
  no markdown, just natural spoken paragraphs. Don't include stage directions like [pause].
- Quote scripture sparingly and only short phrases (a handful of words) — paraphrase the verse
  content in your own words rather than reading it verbatim at length.
- Open with a quick, engaging hook related to the chapter — not "Today we're studying..."
- Some days the reading text you receive is actually several short pieces stitched together,
  each marked with a line like "=== Title Page ===". When you see these markers, move through
  them in order as one flowing episode (not five separate mini-episodes) — treat the whole thing
  as a single reading with several short movements, and aim for the fuller end of the length
  range (20-25 minutes) since there's more ground to cover.
"""


def generate_commentary(title: str, chapter_text: str, api_key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = f"""Today's reading is: {title}

Here is the chapter text for your reference (verse numbers included):

{chapter_text}

Write today's full spoken study script now."""

    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


# ---------------------------------------------------------------------------
# 4. TEXT TO SPEECH (OpenAI), chunked + concatenated into one MP3
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = 3800):
    """Splits on paragraph breaks, keeping chunks under OpenAI's ~4096 char limit."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        candidate = f"{current}\n\n{p}" if current else p
        if len(candidate) > max_chars:
            if current:
                chunks.append(current)
            current = p
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def text_to_speech(script_text: str, out_path: str, api_key: str, voice: str = "fable"):
    from pydub import AudioSegment

    chunks = chunk_text(script_text)
    segment_paths = []

    for i, chunk in enumerate(chunks):
        resp = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "tts-1-hd", "voice": voice, "input": chunk},
            timeout=120,
        )
        resp.raise_for_status()
        seg_path = f"/tmp/segment_{i}.mp3"
        with open(seg_path, "wb") as f:
            f.write(resp.content)
        segment_paths.append(seg_path)
        time.sleep(0.5)  # be polite to the API

    combined = AudioSegment.empty()
    for path in segment_paths:
        combined += AudioSegment.from_mp3(path)
        combined += AudioSegment.silent(duration=400)  # small breathing gap
    combined.export(out_path, format="mp3", bitrate="96k")


# ---------------------------------------------------------------------------
# 5. PUBLISH TO PODCAST FEED (GitHub Pages)
# ---------------------------------------------------------------------------

DOCS_DIR = "docs"
EPISODES_DIR = os.path.join(DOCS_DIR, "episodes")
MANIFEST_PATH = os.path.join(DOCS_DIR, "episodes.json")
FEED_PATH = os.path.join(DOCS_DIR, "feed.xml")


def get_pages_base_url() -> str:
    """Derives the GitHub Pages URL from the repo GitHub Actions runs in.
    Standard project-page format: https://{owner}.github.io/{repo}/"""
    repo_full = os.environ["GITHUB_REPOSITORY"]  # e.g. "yourname/bom-study"
    owner, repo = repo_full.split("/")
    return f"https://{owner}.github.io/{repo}/"


def slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def publish_episode(title: str, mp3_path: str, description: str):
    import datetime
    from feedgen.feed import FeedGenerator
    from pydub import AudioSegment

    os.makedirs(EPISODES_DIR, exist_ok=True)
    base_url = get_pages_base_url()

    # load or start the episode manifest
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH) as f:
            episodes = json.load(f)
    else:
        episodes = []

    ep_num = len(episodes) + 1
    filename = f"{ep_num:04d}-{slugify(title)}.mp3"
    dest_path = os.path.join(EPISODES_DIR, filename)
    os.replace(mp3_path, dest_path)

    duration_seconds = int(len(AudioSegment.from_mp3(dest_path)) / 1000)
    file_size = os.path.getsize(dest_path)
    pub_date = datetime.datetime.now(datetime.timezone.utc).isoformat()

    episodes.append(
        {
            "title": title,
            "filename": filename,
            "description": description,
            "pub_date": pub_date,
            "duration_seconds": duration_seconds,
            "file_size": file_size,
        }
    )
    with open(MANIFEST_PATH, "w") as f:
        json.dump(episodes, f, indent=2)

    # rebuild the whole feed from the manifest -- simple and always consistent
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title("Book of Mormon Daily Study")
    fg.link(href=base_url, rel="alternate")
    fg.description("Daily institute-style Book of Mormon commentary, one chapter at a time.")
    fg.language("en")
    fg.image(base_url + "artwork.jpg")
    fg.podcast.itunes_image(base_url + "artwork.jpg")
    fg.podcast.itunes_category("Religion & Spirituality", "Christianity")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_author("Daily Study Bot")

    for ep in episodes:
        fe = fg.add_entry()
        fe.id(base_url + "episodes/" + ep["filename"])
        fe.title(ep["title"])
        fe.description(ep["description"])
        fe.enclosure(
            base_url + "episodes/" + ep["filename"],
            str(ep["file_size"]),
            "audio/mpeg",
        )
        fe.pubDate(ep["pub_date"])
        fe.podcast.itunes_duration(ep["duration_seconds"])

    fg.rss_file(FEED_PATH)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print chapter + script, skip TTS/email/state update")
    args = parser.parse_args()

    syllabus = build_syllabus()

    with open(STATE_FILE) as f:
        state = json.load(f)
    index = state["index"] % len(syllabus)
    today = syllabus[index]

    if "uris" in today:
        print(f"Today's reading: {today['title']} (combined: {', '.join(today['uris'])})")
        chapter_text = fetch_combined_text(today["uris"], today["section_labels"])
    else:
        print(f"Today's reading: {today['title']} ({today['uri']})")
        chapter_text = fetch_chapter_text(today["uri"])
    print(f"Fetched {len(chapter_text)} characters of chapter text.")

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    script_text = generate_commentary(today["title"], chapter_text, anthropic_key)
    print("\n----- GENERATED SCRIPT -----\n")
    print(script_text)
    print(f"\n(~{len(script_text.split())} words)")

    if args.dry_run:
        print("\n[dry run] Skipping TTS and feed publish.")
        return

    openai_key = os.environ["OPENAI_API_KEY"]
    mp3_path = "/tmp/study.mp3"
    text_to_speech(script_text, mp3_path, openai_key)
    print(f"Wrote {mp3_path}")

    description = f"Institute-style daily study: {today['title']}."
    publish_episode(today["title"], mp3_path, description)
    print("Published to podcast feed (docs/feed.xml).")

    state["index"] = index + 1
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"State advanced to index {state['index']}.")


if __name__ == "__main__":
    main()
