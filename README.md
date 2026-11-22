# GTA Story-Dub Agent 🎮🎙️ (v4: autopilot)

Every run, the agent:
1. **Finds fresh GTA uploads** (GTA VI, or GTA V until VI is out) from your creators on YouTube and Kick, and ranks them by **views per hour** so it catches what's taking off.
2. **Watches the top 3–4 videos completely.** Parts are watched in parallel. Whatever is in the original audio, someone's voiceover included, is ignored, and every usable scene is logged.
3. **Plans a 10–15 minute story episode** built from scenes across all those videos:
   - It picks the genre that fits the footage: gangster drama, desi comedy, classy gentleman, heist, romance and so on.
   - It invents fresh characters, each with a matching voice: a gruff don, a nervous sidekick, a posh gentleman.
   - It opens with a **cold-open hook**: the most dramatic moment as a flash-forward, with narration, so the first 30–50 seconds keep people watching.
4. **Plans 2–3 Shorts**, each with a hook in the first 1–2 seconds and a payoff at the end.
5. **Writes and voices everything** in natural Hinglish, then mixes, captions and renders it. Episodes are 1920×1080; Shorts are 1080×1920.
6. **Schedules the uploads on your YouTube channel** at peak times: the long video at 7:30 pm and Shorts at 12:30, 4:30 and 9:30 (Indian time).
7. It also runs a **trend radar** (today's most-viewed GTA videos), which gives the writers fresh angles, and fills **`outreach.csv`** with channels to ask for permission, each with a ready-made message.

---

## 1. One-time setup (Windows, Command Prompt)

```cmd
winget install Gyan.FFmpeg --source winget
winget install DenoLand.Deno --source winget
cd /d C:\clip-agent\gta-dub-agent
pip install -r requirements.txt
pip install pip-system-certs
copy .env.example .env
notepad .env
```
In `.env`, paste your `GEMINI_API_KEY` (free from https://aistudio.google.com/apikey) and a `SARVAM_API_KEY` as the backup voice engine (https://dashboard.sarvam.ai).

Put 5–10 royalty-free music tracks in `assets\music` (the YouTube Audio Library is free). Then check the setup:
```cmd
python -m dub_agent check
```

## 2. Add your creators (the autopilot only takes footage from these channels)

```cmd
python -m dub_agent add-creator "https://www.youtube.com/@theirhandle" "WhatsApp 16 Nov - OK, credit + link"
python -m dub_agent add-creator "https://kick.com/theirname" "Kick DM 16 Nov - OK"
```
The agent looks up the channel ID itself. To grow your list, open `outreach.csv` after a run. It lists today's hottest GTA channels with a permission message ready to copy and send. Add each one with `add-creator` when they say yes.

### New channels are added automatically (status: pending)

Every gameplay channel the trend radar confirms gets added to `creators.yaml` as `status: pending`, and its fresh videos are used in the **same run**. Videos that use a pending channel's footage are **made but held**: each held folder gets an `AWAITING_PERMISSION.txt`. Once you approve the channel, the next autopilot run uploads them at the next free slot.

```cmd
python -m dub_agent pending                                   (who is waiting, their link, a ready WhatsApp message)
python -m dub_agent approve "Grand Mini" "WhatsApp 19 Nov - OK"   (held videos upload on the next run)
python -m dub_agent approve "Grand Mini" "WhatsApp 19 Nov - OK" --upload-now
python -m dub_agent reject "Some Channel"                     (said no: never used again)
```
To turn this off, set `discover.auto_add_creators: false` or `autopilot.use_radar_videos: false` in `config.yaml`.

## 3. Connect your YouTube channel (one time, about 10 minutes)

1. Go to https://console.cloud.google.com, create a project, then go to **APIs & Services → Library** and enable **YouTube Data API v3**.
2. Under **OAuth consent screen**, choose "External", fill in the app name and your email, and add your Google account as a **Test user**.
3. Under **Credentials → Create credentials → OAuth client ID**, choose "Desktop app", then **Download JSON**. Save the file as `client_secret.json` in the agent folder.
4. Run:
   ```cmd
   python -m dub_agent auth
   ```
   A browser window opens. Pick your channel and allow access. You should then see "Connected to YouTube channel: …".

To use custom thumbnails, verify your channel with a phone number at https://www.youtube.com/verify.

## 4. Run it

```cmd
python -m dub_agent discover              (preview: trending today + fresh uploads it would pick)
python -m dub_agent autopilot --dry-run   (shows the picks, makes nothing)
python -m dub_agent autopilot --no-upload (makes the videos so you can watch them first)
python -m dub_agent autopilot             (makes + schedules uploads)
```

**Fully automatic:** this runs the autopilot every 2 hours, even when you're not there:
```cmd
python -m dub_agent schedule --every 2
```
Check `output\autopilot_log.txt` to see what it made and scheduled. To stop it, run `schtasks /delete /tn "GTA Dub Autopilot" /f`.

### Manual mode (any video links you choose)
```cmd
python -m dub_agent run "URL1" "URL2" "URL3" --format both --minutes 12 --shorts 3
python -m dub_agent run "URL" --format episode
python -m dub_agent upload "output\20261120_1930_autopilot\episode"          (next free slot)
python -m dub_agent upload "output\...\short_01" --at "2026-11-21 12:30"
```

## How discovery filters videos (precise mode)

Every video, whether from your creators or from the trend radar, goes through this funnel:

1. **Newest-first search**, using the real upload time of each video (not YouTube's "today" filter, which is unreliable).
2. **Right game:** GTA V until 19 Nov 2026, then GTA VI automatically (`discover.target_game`). "GTA 6 news" titles are rejected while GTA V is the target. San Andreas, BeamNG, Minecraft and other games are rejected.
3. **Junk words rejected:** trailer, leak, news, theory, reaction, unboxing, "downloaded", gaming room, vlog, review, top 10, memes, best moments, compilation, AI/animation, Shinchan/Doraemon/McQueen mashups, #shorts, and more. Add your own in `exclude_terms`.
4. **Fresh and full-length:** uploaded within 24 h for your creators (48 h for the radar), 6 minutes to 4 hours long, not live or upcoming, and in the Gaming category.
5. **AI check:** Gemini looks at the **thumbnail** plus the title, description and tags, and confirms all of these:
   - it's real gameplay of the right game
   - it's **faceless** (a small corner facecam is OK)
   - it's not AI or memes, and not a vlog or news
   - it rates at least 5/10 as story footage

   Results are cached, so a video is only checked once.
6. **Ranked by views per hour.**

**`discover_report.csv`** lists every video it looked at, with **KEEP/REJECT and the reason**, e.g. "REJECT AI: face-cam/creator on screen" or "REJECT too old (200 days)". If a good video gets rejected, add a word to `exclude_terms` or adjust `min_quality`.

Kick: footage from your Kick creators is checked the same way. Kick has no public search for past broadcasts, so the trend radar covers YouTube only. Add Kick streamers you find yourself with `add-creator`.

## How the fresh-content strategy works

| Setting | Default | Why |
|---|---|---|
| Check every | 2 hours | GTA uploads peak in the evening (Indian time), and launch week moves fast |
| `max_age_hours` | 24 | Only footage from the last day, so your stories stay on what people are watching now |
| `rank_by` | velocity (views per hour) | A 1-hour-old video with 20k views beats a 2-day-old one with 100k: it's the next big thing |
| `pick_top` | 4 videos | Enough variety for a 12-minute episode plus 3 Shorts |
| Publishing | 1 long + 3 Shorts a day | Long video at 19:30 for the evening peak; Shorts spread across the day |
| `max_queue_days` | 2 | If upload slots are already booked 2 days ahead, the run skips making more (saves API quota) |
| Trend radar | today's most-viewed GTA videos | The writers see what's hot (characters, missions, memes) and build fresh stories around it |

The YouTube API allows about **6 uploads a day** on the default quota, and the schedule above uses 4. Change `upload.long_slots` and `upload.short_slots` in `config.yaml` to change the times.

## Editing before it goes public

Every upload is **private and scheduled**, so you can review it in YouTube Studio first. To fix a line, edit `script.json` in that folder and re-render:
```cmd
python -m dub_agent rerender "output\FOLDER\episode"
```
If you re-render something that was already uploaded, delete the old upload in YouTube Studio, delete `uploaded.json` in the folder, then run `upload` again.

## Speed

- **The AI watches a small 360p copy** that's downloaded directly. The full-quality download runs in the background at the same time.
- **Parts of each video are watched in parallel**, and voice lines are generated 4 at a time.
- **GPU encoding** is detected automatically (NVIDIA, Intel or AMD), which renders 3–10× faster. The log line "Video encoder: …" shows which one is in use.
- **Everything is cached**: scene logs, episode scripts and voice lines. A rerun only redoes what changed.
- On Gemini's paid tier, raise `performance.scan_workers` to 3–4.

## YouTube bot check ("Sign in to confirm you're not a bot")

YouTube blocks anything that makes lots of requests without being logged in. Give the agent your YouTube login once:

**Option A: cookies.txt (recommended on Windows)**
1. In Chrome or Edge, install the extension **"Get cookies.txt LOCALLY"** from the Chrome Web Store.
2. Open https://www.youtube.com while logged in. Using a spare Google account is a good idea.
3. Click the extension and choose **Export**, then save the file as **`cookies.txt`** in the agent folder (next to `config.yaml`).
4. That's it. The agent picks up `cookies.txt` automatically.

Re-export the file if the bot error comes back. Cookies expire after a few weeks, or sooner if you log out of that browser.

**Option B: Firefox.** Log in to YouTube in Firefox, then set `discover.cookies_from_browser: firefox` in `config.yaml`. Chrome and Edge are not supported here because their newer cookie encryption blocks this on Windows.

Without cookies, the agent waits 60 seconds between searching and downloading, retries a blocked video once, and skips videos it still can't open. It no longer crashes the whole run.

## Troubleshooting

| Problem | Fix |
|---|---|
| Certificate errors | `pip install pip-system-certs`, or run on your home network |
| "No supported JavaScript runtime" | `winget install DenoLand.Deno --source winget`, then open a new Command Prompt |
| "YouTube refused N requests … not a bot" | Too many requests from your connection. Wait 30–60 min, or set `discover.cookies_from_browser: firefox` (or `edge` / `chrome`) in `config.yaml`, using a browser where you're logged in to YouTube. Chrome must be fully closed for this to work |
| "No fresh GTA uploads" | Your creators haven't posted GTA videos in the last 24 h. Add more creators, or raise `discover.max_age_hours` |
| Kick list unavailable | Kick sometimes blocks automated requests. YouTube still works; try again later |
| "YouTube login expired" | Run `python -m dub_agent auth` once by hand |
| Upload quota reached | The agent uploads the remaining videos automatically on the next run |
| Gemini 429 / quota | It waits and retries, and voices switch to Sarvam automatically |

## Testing without any API keys
```cmd
python -m dub_agent run video.mp4 --allow-unlisted --moments "10-35" --engine dummy --script examples\sample_script.json
```


## News mode (v6) — one command, daily videos

```
python -m dub_agent make
```
Finds today's hot gaming + entertainment topics, researches facts with Google Search (Gemini free key),
finds matching footage on YouTube, writes a Hindi script, voices it (Gemini → Edge fallback), cuts the video
to the narration and writes `upload.txt` for manual upload.  Output: `output/<date>_daily/long_01`, `short_01..03`.

Options: `--topic "..."`, `--shorts 2`, `--no-long`, `--minutes 8`, `--focus gaming|entertainment`, `--footage URL ...`, `--engine edge`.
Fix a script: edit `script.json` in the folder, then `python -m dub_agent rerender <folder>`.
New footage channels are added to creators.yaml as `pending`; mark `status: approved` and those videos become uploadable.
