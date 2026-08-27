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
  3. Asks Claude to write today's ~5-7 minute leadership commentary from
     that day's slice
  4. Converts it to an MP3 with OpenAI TTS
  5. Publishes it to its own podcast feed (docs/come-follow-lead/feed.xml)
  6. Advances the bookmark in come_follow_lead_state.json (the GitHub
     Action commits this back)

No Sunday episode — that's church day.

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
    resp = requests.get(course_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    all_links = soup.find_all("a", href=True)
    for a in all_links:
        link_text = a.get_text(" ", strip=True)
        if len(link_text) < 10:
            continue
        if _date_range_contains(link_text, today):
            href = a["href"]
            if href.startswith("/"):
                href = CFM_BASE + href
            return href.split("?")[0] + "?lang=eng"

    # DIAGNOSTIC BLOCK -- temporary, to see why no match was found.
    # Remove this once the real fix is in.
    print("=== DIAGNOSTIC: no matching lesson link found ===")
    print(f"Total <a> tags found: {len(all_links)}")
    print(f"Does raw HTML contain 'August 24'? {'August 24' in resp.text}")
    print(f"Does raw HTML contain '__NEXT_DATA__' or 'application/json'? "
          f"{'__NEXT_DATA__' in resp.text or 'application/json' in resp.text}")
    print("First 15 non-trivial link texts found:")
    shown = 0
    for a in all_links:
        t = a.get_text(" ", strip=True)
        if len(t) >= 5:
            print(f"  - {t[:80]!r}  ->  {a.get('href')}")
            shown += 1
        if shown >= 15:
            break
    print("=== First 2000 chars of raw HTML ===")
    print(resp.text[:2000])
    print("=== END DIAGNOSTIC ===")

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

Respond with ONLY a single JSON object, no other text, no markdown fences, in exactly this shape:

{
  "week_start_date": "YYYY-MM-DD",
  "course_of_study": "as given",
  "full_scripture_reference": "e.g. Psalms 49-51; 61-66; 69-72; 77-78; 85-86",
  "lesson_title": "the manual's own title for the week, in quotes as given",
  "days": [
    {"day_number": 1, "weekday": "Monday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."},
    {"day_number": 2, "weekday": "Tuesday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."},
    {"day_number": 3, "weekday": "Wednesday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."},
    {"day_number": 4, "weekday": "Thursday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."},
    {"day_number": 5, "weekday": "Friday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."},
    {"day_number": 6, "weekday": "Saturday", "scripture_slice": "...", "focus_note": "...", "source_excerpt": "..."}
  ]
}
"""


def extract_json_object(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in response:\n{text}")
    return json.loads(text[start : end + 1])


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
        messages=[{"role": "user", "content": user_prompt}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    plan = extract_json_object(raw)
    required = {"week_start_date", "course_of_study", "full_scripture_reference", "lesson_title", "days"}
    missing = required - plan.keys()
    if missing:
        raise ValueError(f"Week plan missing keys {missing}. Raw response:\n{raw}")
    if len(plan["days"]) != 6:
        raise ValueError(f"Expected 6 days in week plan, got {len(plan['days'])}. Raw:\n{raw}")
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
Each week's reading is divided across six short episodes, Monday through Saturday (no Sunday --
that's the day he's at church using what the week already taught him). Each daily episode:
- Covers only that day's assigned slice, not the whole week.
- Targets roughly 700-1,000 words (about 5-7 minutes spoken).
- Opens fast -- a one- or two-line hook. He's getting in the truck; get to the point.
- Explains the day's passage in plain language, then lands on ONE clear leadership idea from it
  (not four -- save the range for the week as a whole). Let each day's takeaway come naturally
  from that day's actual text rather than forcing the same structure every day.
- Connects that idea to at least one of: leading his crew/company, leading his soldiers, or
  leading/raising his two boys -- whichever fits best that day, not all three crammed
