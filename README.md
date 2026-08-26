# Daily Book of Mormon Study

Every day this generates a ~20 minute institute-style commentary (Bytheway
humor + Elder Kearon's warmth, Extreme Ownership parallels where they
genuinely fit, always circling back to joy) on the next chapter of the Book
of Mormon — starting at the Title Page/Introduction, then 1 Nephi 1 onward —
and publishes it as a new episode in your own private podcast feed, so it
shows up in whatever podcast app you already use for the drive to work.

## One-time setup (about 20 minutes)

### 1. Get two API keys

| Service | What it's for | Where to get the key |
|---|---|---|
| **Anthropic** | Writes the commentary script | console.anthropic.com → API Keys |
| **OpenAI** | Converts the script to speech (MP3) | platform.openai.com → API Keys |

Expect to spend well under $1/month total.

### 2. Create a GitHub repo — this one needs to be **public**

GitHub Pages (the free file host this uses to serve your MP3s and podcast
feed) only works on the free tier if the repo is public. Nothing in this
repo is personal — it's just scripture commentary audio — but the feed URL
would technically be reachable by anyone who had the exact link. In
practice no one will stumble onto it since it's never listed anywhere.

- Create a new **public** repo on GitHub (e.g. `bom-study`)
- Upload every file in this folder, keeping the folder structure intact —
  `.github/workflows/daily-study.yml` and everything under `docs/` need to
  stay at their exact paths

### 3. Turn on GitHub Pages

- In the repo: **Settings → Pages**
- Under "Build and deployment," set **Source** to "Deploy from a branch"
- Set **Branch** to `main` and the folder to `/docs` → **Save**
- GitHub will show you the live URL, something like
  `https://yourname.github.io/bom-study/` — that's your show's home

### 4. Add your secrets

**Settings → Secrets and variables → Actions → New repository secret**:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`

(No email service needed anymore — the podcast feed replaces that.)

### 5. Test it

**Actions** tab → "Daily Book of Mormon Study" → **Run workflow**. Give it
a minute or two (TTS takes a bit). When it finishes, check
`https://yourname.github.io/bom-study/feed.xml` in a browser — you should
see XML with your first episode in it.

### 6. Subscribe in your podcast app

Most podcast apps have an "Add by URL" or "Subscribe by RSS" option (look
under Settings, or a "+" menu):

- **Apple Podcasts** (iPhone): Library tab → "..." menu → Follow a Show by URL
- **Pocket Casts**: Discover → search icon → paste URL
- **Overcast**: "+" → Add URL
- **Podcast Addict** (Android): "+" → Add by feed URL
- **Spotify** does not support custom RSS feeds — use one of the above instead

Paste in `https://yourname.github.io/bom-study/feed.xml` and subscribe.
Turn on auto-download and notifications for that show, same as you would
for any other podcast — new episodes will just be waiting for you.

## How the "bookmark" works

`state.json` tracks which chapter is next. `docs/episodes.json` is the
running list of everything published so far, and `docs/feed.xml` is
rebuilt from that list on every run — so the feed is always consistent
even if a run fails partway. Episodes themselves live as MP3 files under
`docs/episodes/`.

## Changing the schedule

Edit the `cron` line in `.github/workflows/daily-study.yml` — cron time is
always UTC. Currently set to 11:00 UTC. If you want it done well before
your morning drive, aim for a couple hours earlier than you leave, to give
the workflow (and your podcast app's background refresh) time to run.

## If the scripture scraper breaks

`fetch_chapter_text()` in `main.py` scrapes churchofjesuschrist.org's
public study pages. If the Church's site changes its HTML structure, this
is the one function to fix. Test locally with:
```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python main.py --dry-run
```
`--dry-run` prints the fetched chapter and generated script without
spending money on TTS or touching the feed.

## Tuning the style

All the "voice" instructions live in the `SYSTEM_PROMPT` string near the
top of `main.py` — humor level, the Kearon/Bytheway blend, how often
Extreme Ownership or war stories show up, the joy throughline, target
length. Edit that prompt directly; no code restructuring needed.
