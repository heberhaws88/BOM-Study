"""
Come, Follow, Lead — Daily Leadership Commentary
-------------------------------------------------
Same pipeline shape as main.py (the Book of Mormon show) in this same repo,
publishing to its own subfolder (docs/come-follow-lead/) so the two shows
never collide.

Each run:
  1. Figures out the current Come Follow Me week (state file remembers it)
  2. On the first day of a new week, scrapes that week's lesson from
     churchofjesuschrist.org and asks Claude to split it into a
     Monday-Saturday arc
  3. Asks Claude to write today's ~16-20 minute leadership commentary from
     that day's slice
  4. Converts it to an MP3 with OpenAI TTS
  5. Publishes it to its own podcast feed (docs/come-follow-lead/feed.xml)
  6. Advances the bookmark in come_follow_lead_state.json (the GitHub
     Action commits this back)

Sunday is different: instead of a new day's slice, it generates a full
recap of the whole week (Monday-Saturday) so he can listen to it on the
way to church and walk in already remembering the week's material.

Run with --dry-run to print the fetched lesson + generated script WITHOUT
calling TTS or touching the feed or advancing state.
"""

import os
import re
import sys
import json
import time
import argparse
import datetime
import requests
from bs4 import BeautifulSoup

STATE_FILE = "come_follow_lead_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ComeFollowLeadBot/1.0)"}

CFM_BASE = "https://www.churchofjesuschrist.org"
CFM_INDEX_URL = f"{CFM_BASE}/study/come-follow-me?lang=eng"

DATE_RANGE_RE = re.compile(
    r"([A-Z][a-z]+)\s+(\d{1,2})\s*[–—-]\s*(?:([A-Z][a-z]+)\s+)?(\d{1,2})"
)
MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}

# ---------------------------------------------------------------------------
# 1. CONTENT SOURCE: scrape the current week's Come Follow Me lesson
# ---------------------------------------------------------------------------

def _get_soup(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # the site doesn't always declare charset in headers;
    # without this, requests guesses Latin-1 and mangles the en-dash in date
    # ranges like "August 24-30" into garbage bytes.
    return BeautifulSoup(resp.text, "html.parser")


def find_current_course_url(year: int) -> str:
    """Finds this year's Come Follow Me manual root URL from the landing page
    (the course rotates yearly: Old Testament / New Testament / Book of
    Mormon / Doctrine and Covenants)."""
    soup = _get_soup(CFM_INDEX_URL)
    pattern = re.compile(rf"/study/manual/come-follow-me-for-home-and-church-[a-z\-]+-{year}")
    for a in soup.find_all("a", href=True):
        if pattern.search(a["href"]):
            href = a["href"]
            if href.startswith("/"):
                href = CFM_BASE + href
            return href.split("?")[0] + "?lang=eng"
    raise RuntimeError(
        f"Couldn't find a {year} Come Follow Me manual link on {CFM_INDEX_URL}. "
        "The landing page structure may have changed — inspect it manually."
    )


def _date_range_contains(text: str, today: datetime.date) -> bool:
    match = DATE_RANGE_RE.search(text)
    if not match:
        return False
    start_month_name, start_day, end_month_name, end_day = match.groups()
    start_month = MONTHS.get(start_month_name)
    end_month = MONTHS.get(end_month_name) if end_month_name else start_month
    if start_month is None or end_month is None:
        return False
    try:
        start = datetime.date(today.year, start_month, int(start_day))
        end = datetime.date(today.year, end_month, int(end_day))
    except ValueError:
        return False
    if end < start:  # week spans a year boundary (Dec -> Jan)
        return today >= start or today <= end
    return start <= today <= end


def find_week_lesson_url(course_url: str, today: datetime.date) -> str:
    """Walks the course's table of contents to find the lesson whose date
    range (e.g. "August 24-30") contains today."""
    soup = _get_soup(course_url)
    for a in soup.find_all("a", href=True):
        link_text = a.get_text(" ", strip=True)
        if len(link_text) < 10:
            continue
        if _date_range_contains(link_text, today):
            href = a["href"]
            if href.startswith("/"):
                href = CFM_BASE + href
            return href.split("?")[0] + "?lang=eng"
    raise RuntimeError(
        f"Couldn't find a lesson on {course_url} covering {today.isoformat()}. "
        "The manual's table-of-contents structure may have changed — inspect it manually."
    )


def extract_lesson_text(soup: BeautifulSoup) -> str:
    """Same selector strategy proven to work against churchofjesuschrist.org
    in fetch_chapter_text() in main.py, plus a generic fallback."""
    body = soup.select_one("div#study-content, div.body-block, main")
    if body:
        paragraphs = body.find_all(["h1", "h2", "h3", "p", "li"])
        text = "\n\n".join(p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True))
        if len(text) > 300:
            return text

    # Generic fallback if the site's markup doesn't match the above.
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    blocks = [
        el.get_text(" ", strip=True)
        for el in soup.find_all(["h1", "h2", "h3", "p", "li"])
        if len(el.get_text(strip=True)) >= 25
    ]
    text = "\n\n".join(dict.fromkeys(blocks))  # dedupe, keep order
    if len(text) < 300:
        raise RuntimeError(
            "Extracted lesson text looks too short to be real content — "
            "the page structure has likely changed. Inspect the page manually."
        )
    return text


def fetch_current_week(today: datetime.date) -> dict:
    course_url = find_current_course_url(today.year)
    lesson_url = find_week_lesson_url(course_url, today)
    soup = _get_soup(lesson_url)
    raw_text = extract_lesson_text(soup)
    course_of_study = (
        course_url.rstrip("/").split("/")[-1]
        .replace("come-follow-me-for-home-and-church-", "")
        .replace("-", " ")
        .title()
    )
    return {"course_of_study": course_of_study, "lesson_url": lesson_url, "raw_text": raw_text}


# ---------------------------------------------------------------------------
# 2. WEEK PLANNING: split the week's reading into a Monday-Saturday arc
# ---------------------------------------------------------------------------

WEEK_PLANNER_PROMPT = """You are preparing the week's schedule for a Come Follow Me podcast. You
will be given today's date, the current course of study, the source URL, and the raw scraped text
of this week's Come Follow Me manual lesson.

Your job, using ONLY the provided text (do not invent scripture content that isn't grounded in it):

1. Identify the lesson's title (as the manual states it, in quotes) and the full scripture
   reference for the week (e.g. "Psalms 49-51; 61-66; 69-72; 77-78; 85-86").
2. Divide the week's reading into six roughly-even daily segments for Monday through Saturday.
   Split by natural chapter/section breaks, not strict verse-count math -- favor keeping a
   chapter or a clear sub-story together on one day over splitting it mid-thought.
3. For each day, pull out the specific excerpt of the provided source text relevant to that
   day's slice -- enough that a script writer could work from it without the full week's text.

Call the submit_week_plan tool with your answer.
"""

# Using a tool call (structured output) instead of asking Claude to hand-write JSON --
# scripture text is full of quotation marks, and a model handwriting raw JSON around that
# text is prone to producing near-miss JSON. The API validates/encodes this properly.
WEEK_PLAN_TOOL = {
    "name": "submit_week_plan",
    "description": "Submit the parsed weekly Come Follow Me schedule, split into a Monday-Saturday arc.",
    "input_schema": {
        "type": "object",
        "properties": {
            "week_start_date": {"type": "string", "description": "YYYY-MM-DD, the Monday of this week"},
            "course_of_study": {"type": "string"},
            "full_scripture_reference": {"type": "string"},
            "lesson_title": {"type": "string"},
            "days": {
                "type": "array",
                "minItems": 6,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "day_number": {"type": "integer"},
                        "weekday": {"type": "string"},
                        "scripture_slice": {"type": "string"},
                        "focus_note": {"type": "string"},
                        "source_excerpt": {"type": "string"},
                    },
                    "required": ["day_number", "weekday", "scripture_slice", "focus_note", "source_excerpt"],
                },
            },
        },
        "required": ["week_start_date", "course_of_study", "full_scripture_reference", "lesson_title", "days"],
    },
}


def plan_week(today: datetime.date, api_key: str) -> dict:
    import anthropic

    source = fetch_current_week(today)
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = f"""Today's date is {today.isoformat()} ({today.strftime('%A')}).
Course of study: {source['course_of_study']}
Source URL: {source['lesson_url']}

Raw scraped text of this week's Come Follow Me manual lesson:
---
{source['raw_text']}
---

Plan this week's episodes as instructed."""

    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        system=WEEK_PLANNER_PROMPT,
        tools=[WEEK_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_week_plan"},
        messages=[{"role": "user", "content": user_prompt}],
    )
    plan = None
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_week_plan":
            plan = block.input
            break
    if plan is None:
        raise RuntimeError(f"Claude didn't return a submit_week_plan tool call. Response: {resp.content}")
    if len(plan["days"]) != 6:
        raise ValueError(f"Expected 6 days in week plan, got {len(plan['days'])}. Plan: {plan}")
    return plan


# ---------------------------------------------------------------------------
# 3. GENERATE TODAY'S SCRIPT
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the writer and host voice for "Come, Follow, Lead" -- a short daily
podcast episode built for one specific listener. Write every episode as continuous spoken-word
prose, in the second person, as if you are talking directly to him on a drive.

WHO YOU'RE TALKING TO:
He's a husband and father of two boys (one just starting junior high). He's a licensed
electrician who owns his own electrical company, and he serves as a soldier in the Army National
Guard. He follows the Church of Jesus Christ of Latter-day Saints' Come Follow Me schedule and
wants to arrive at church on Sunday already understanding the week's reading -- not hearing it
cold. He has said plainly that he isn't confident reading and interpreting scripture on his own,
so always translate the passage into plain language before building any application on top of
it. Never treat that as a deficiency -- treat it as completely normal.

Everything comes back to leading people well: his crew, his squad, and above all his own home.
The throughline every episode is what this passage teaches him about leading the people who are
watching him -- and specifically about raising his two boys toward becoming disciples of Jesus
Christ.

VOICE: a blend of John Bytheway and Thomas S. Monson.
- Bytheway side: conversational, warm, a little funny, talks like a real person, not a pulpit.
  Breaks things into plain, memorable language. Uses everyday analogies -- jobsite, drill
  weekend, dinner table -- instead of academic language.
- Monson side: leads with story before doctrine. Personal, direct address ("you," never "one" or
  "we should"). Bears simple, sincere testimony rather than arguing a point. Circles back to
  service and to the people right in front of you as the real proving ground of faith. Closes
  with a clear, doable call to action.
- Never invent specific autobiographical claims about his past ("remember when you..."). Use
  open, relatable framing instead ("you know that feeling when...") and invite him to supply his
  own memory.
- Write in full, flowing sentences meant to be heard, not read. No headers, bullet points,
  markdown, or stage directions -- the entire output is what gets spoken aloud, start to finish.

FORMAT: this is a DAILY episode, not the full week in one sitting.
Each week's reading is divided across six episodes, Monday through Saturday. (Sunday gets a
separate full-week review episode, generated by a different prompt below -- so don't try to
recap the whole week here; just cover today's slice well.) Each daily episode:
- Covers only that day's assigned slice, not the whole week -- but goes deep on that slice rather
  than padding. Walk through the passage more thoroughly: don't just make one quick point and
  move on. Go verse by verse or moment by moment through what's actually happening, in plain
  language, before layering in application.
- Targets roughly 2,400-3,000 words (about 16-20 minutes spoken). Reach this length through real
  substance, not repetition or filler -- more of the actual text explained, two or three distinct
  angles or stories, not one idea stretched thin.
- Opens with a real hook -- a story, a scenario, or a vivid question -- not "Today we're studying..."
- Explains the day's passage in plain language as you go, then draws out two or three leadership
  ideas from it across the episode (more room today than a short episode allows). Let each day's
  takeaways come naturally from that day's actual text rather than forcing a fixed template.
- Connects those ideas to a mix of: leading his crew/company, leading his soldiers, and
  leading/raising his two boys -- weave between them across the episode rather than picking only one.
- Include one or two outside stories or analogies where they genuinely fit (a jobsite moment, a
  military scenario, a historical or well-known story) to illustrate a point -- don't force one in
  if the passage doesn't call for it.
- Ends with a clear, doable challenge for that day -- one or two sentences, specific.

Ground everything in the source excerpt you're given -- it's the real manual text, not a summary
you're free to depart from. Do not fabricate scripture content, verses, or citations that aren't
supported by it.

Output ONLY the finished spoken script. No title, no preamble, no markdown.
"""


def generate_script(week: dict, day_info: dict, covered_so_far: list, api_key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    covered_note = (
        "Nothing covered yet this week -- this is day 1."
        if not covered_so_far
        else "Already covered earlier this week: " + "; ".join(covered_so_far)
    )
    user_prompt = f"""This week's full Come Follow Me reading: {week['full_scripture_reference']}
Lesson title: {week['lesson_title']}
Course of study: {week['course_of_study']}

Today is {day_info['weekday']} -- day {day_info['day_number']} of 6 this week.
Today's specific slice to cover: {day_info['scripture_slice']}
Focus note for today: {day_info.get('focus_note', '')}

Source excerpt from this week's Come Follow Me manual, relevant to today:
---
{day_info.get('source_excerpt', '(none provided)')}
---

{covered_note}

Write today's episode now."""

    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


SUNDAY_SYSTEM_PROMPT = """You are the writer and host voice for "Come, Follow, Lead" -- same show,
same listener, same voice as every other day (a blend of John Bytheway and Thomas S. Monson:
conversational, warm, a little funny, leads with story before doctrine, direct second-person
address, closes with a clear call to action). This one script is different in purpose: it's the
Sunday episode, meant to be listened to on the way to church, after the six daily episodes this
week (Monday-Saturday) have already covered the full reading.

WHO YOU'RE TALKING TO: a husband and father of two boys (one just starting junior high), a
licensed electrician who owns his own electrical company, and a soldier in the Army National
Guard. He isn't confident reading and interpreting scripture on his own -- that's exactly why this
week of daily episodes existed, and why this Sunday episode exists: to walk in the door of church
already remembering what he learned, not hearing it cold.

YOUR JOB TODAY: give him a real review of the whole week, not a new deep dive.
- Open with a short hook about walking into church today already knowing this material -- that's
  the payoff of the whole week.
- Walk back through the week day by day (Monday through Saturday), reminding him briefly what each
  day covered and the one leadership idea that day was really about. This should feel like flipping
  back through the week with him, not re-teaching each day from scratch -- keep each day's recap to
  a few sentences.
- Pull out the single bigger-picture thread that ran underneath the whole week -- the idea that
  connects Monday's passage to Saturday's. Spend real time on this; it's the heart of the episode.
- Tie that thread back to the same three places every episode this week pointed to: leading his
  crew, leading his soldiers, and leading/raising his two boys.
- Close with 3-4 concrete discussion questions he could raise in Sunday School or bring to the
  dinner table today -- specific enough to actually start a conversation, grounded in this week's
  actual reading.

LENGTH: roughly 1,600-2,200 words (about 10-15 minutes spoken) -- shorter than a daily episode on
purpose, since this is review, not new teaching. Don't pad it to sound longer.

Never invent specific autobiographical claims about his past. Write in full, flowing sentences
meant to be heard, not read -- no headers, bullet points, markdown, or stage directions. Ground
everything in the week's actual material below; don't fabricate scripture content or citations.

Output ONLY the finished spoken script. No title, no preamble, no markdown.
"""


def generate_sunday_recap(week: dict, api_key: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    days_summary = "\n\n".join(
        f"Day {d['day_number']} ({d['weekday']}): {d['scripture_slice']}\n"
        f"Focus note: {d.get('focus_note', '')}"
        for d in week["days"]
    )
    user_prompt = f"""This week's full Come Follow Me reading: {week['full_scripture_reference']}
Lesson title: {week['lesson_title']}
Course of study: {week['course_of_study']}

Here is how the week was divided across the six daily episodes:
---
{days_summary}
---

Write today's (Sunday's) full-week review episode now."""

    resp = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=6144,
        system=SUNDAY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


# ---------------------------------------------------------------------------
# 4. TEXT TO SPEECH (OpenAI), chunked + concatenated into one MP3
#    -- same approach as text_to_speech() in main.py
# ---------------------------------------------------------------------------

def chunk_text(text: str, max_chars: int = 3800):
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


def text_to_speech(script_text: str, out_path: str, api_key: str, voice: str = "onyx"):
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
        seg_path = f"/tmp/cfl_segment_{i}.mp3"
        with open(seg_path, "wb") as f:
            f.write(resp.content)
        segment_paths.append(seg_path)
        time.sleep(0.5)

    combined = AudioSegment.empty()
    for path in segment_paths:
        combined += AudioSegment.from_mp3(path)
        combined += AudioSegment.silent(duration=300)
    combined.export(out_path, format="mp3", bitrate="96k")


# ---------------------------------------------------------------------------
# 5. PUBLISH TO ITS OWN PODCAST FEED -- docs/come-follow-lead/
#    (kept separate from docs/ root so the Book of Mormon show is untouched)
# ---------------------------------------------------------------------------

DOCS_DIR = os.path.join("docs", "come-follow-lead")
EPISODES_DIR = os.path.join(DOCS_DIR, "episodes")
MANIFEST_PATH = os.path.join(DOCS_DIR, "episodes.json")
FEED_PATH = os.path.join(DOCS_DIR, "feed.xml")


def get_show_base_url() -> str:
    """Same derivation as get_pages_base_url() in main.py, plus this show's subfolder."""
    repo_full = os.environ["GITHUB_REPOSITORY"]  # e.g. "heberhaws88/BOM-Study"
    owner, repo = repo_full.split("/")
    return f"https://{owner}.github.io/{repo}/come-follow-lead/"


def slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def publish_episode(title: str, mp3_path: str, description: str):
    from feedgen.feed import FeedGenerator
    from pydub import AudioSegment

    os.makedirs(EPISODES_DIR, exist_ok=True)
    base_url = get_show_base_url()

    episodes = json.load(open(MANIFEST_PATH)) if os.path.exists(MANIFEST_PATH) else []

    ep_num = len(episodes) + 1
    filename = f"{ep_num:04d}-{slugify(title)}.mp3"
    dest_path = os.path.join(EPISODES_DIR, filename)
    os.replace(mp3_path, dest_path)

    duration_seconds = int(len(AudioSegment.from_mp3(dest_path)) / 1000)
    file_size = os.path.getsize(dest_path)
    pub_date = datetime.datetime.now(datetime.timezone.utc).isoformat()

    episodes.append({
        "title": title,
        "filename": filename,
        "description": description,
        "pub_date": pub_date,
        "duration_seconds": duration_seconds,
        "file_size": file_size,
    })
    with open(MANIFEST_PATH, "w") as f:
        json.dump(episodes, f, indent=2)

    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title("Come, Follow, Lead")
    fg.link(href=base_url, rel="alternate")
    fg.description(
        "A daily leadership commentary on the Come Follow Me reading -- for a soldier, an "
        "electrician, a business owner, and a dad raising two boys toward Christ."
    )
    fg.language("en")
    fg.podcast.itunes_category("Religion & Spirituality", "Christianity")
    fg.podcast.itunes_explicit("no")
    fg.podcast.itunes_author("Come, Follow, Lead")

    for ep in episodes:
        fe = fg.add_entry()
        fe.id(base_url + "episodes/" + ep["filename"])
        fe.title(ep["title"])
        fe.description(ep["description"])
        fe.enclosure(base_url + "episodes/" + ep["filename"], str(ep["file_size"]), "audio/mpeg")
        fe.pubDate(ep["pub_date"])
        fe.podcast.itunes_duration(ep["duration_seconds"])

    fg.rss_file(FEED_PATH)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"week": None, "day_index": 0, "episodes_this_week": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print script, skip TTS/publish/state update")
    args = parser.parse_args()

    today = datetime.date.today()
    weekday_name = today.strftime("%A")

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    state = load_state()
    monday_of_this_week = today - datetime.timedelta(days=today.weekday())

    need_new_plan = (
        state.get("week") is None
        or state["week"].get("week_start_date") != monday_of_this_week.isoformat()
    )
    if need_new_plan:
        print(f"Planning new week starting {monday_of_this_week.isoformat()}...")
        week = plan_week(today, anthropic_key)
        state["week"] = week
        state["day_index"] = 0
        state["episodes_this_week"] = []
    else:
        week = state["week"]

    if weekday_name == "Sunday":
        print(f"Sunday -- generating full-week review of: {week['lesson_title']}")
        script_text = generate_sunday_recap(week, anthropic_key)
        print("\n----- GENERATED SCRIPT -----\n")
        print(script_text)
        print(f"\n(~{len(script_text.split())} words)")

        if args.dry_run:
            print("\n[dry run] Skipping TTS and feed publish.")
            return

        openai_key = os.environ["OPENAI_API_KEY"]
        mp3_path = "/tmp/come_follow_lead_episode.mp3"
        text_to_speech(script_text, mp3_path, openai_key)
        print(f"Wrote {mp3_path}")

        title = f"Sunday Review: {week['lesson_title']}"
        description = f"A full recap of the week's reading -- {week['full_scripture_reference']} -- to get you ready for church."
        publish_episode(title, mp3_path, description)
        print(f"Published to podcast feed ({FEED_PATH}).")

        save_state(state)
        print("State saved.")
        return

    day_number = today.weekday() + 1  # Monday=1 ... Saturday=6
    day_info = next((d for d in week["days"] if d["day_number"] == day_number), None)
    if day_info is None:
        raise RuntimeError(f"No day plan found for day_number={day_number} in week plan.")

    print(f"{day_info['weekday']} (day {day_number} of 6): {day_info['scripture_slice']}")
    script_text = generate_script(week, day_info, state.get("episodes_this_week", []), anthropic_key)
    print("\n----- GENERATED SCRIPT -----\n")
    print(script_text)
    print(f"\n(~{len(script_text.split())} words)")

    if args.dry_run:
        print("\n[dry run] Skipping TTS and feed publish.")
        return

    openai_key = os.environ["OPENAI_API_KEY"]
    mp3_path = "/tmp/come_follow_lead_episode.mp3"
    text_to_speech(script_text, mp3_path, openai_key)
    print(f"Wrote {mp3_path}")

    title = f"{day_info['weekday']}: {week['lesson_title']} (day {day_number} of 6)"
    description = f"{day_info['scripture_slice']} -- {day_info.get('focus_note', '')}"
    publish_episode(title, mp3_path, description)
    print(f"Published to podcast feed ({FEED_PATH}).")

    state["day_index"] = day_number
    state.setdefault("episodes_this_week", []).append(day_info["scripture_slice"])
    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
