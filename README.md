# Bottle Net Tool

A command-line crawler and downloader for publicly accessible
TikTok and Facebook videos.

Author: Aung Khit Htoo
License: MIT

```text
╔════════════════════════════════════════════════╗
║                BOTTLE NET TOOL                 ║
║       Public Video Crawler & Downloader        ║
║                                                ║
║             Author: Aung Khit Htoo             ║
╚════════════════════════════════════════════════╝
```

Bottle Net Tool (`bottle-net`) can:

- **Crawl** a public TikTok account or Facebook Page and collect its video links.
- **Download** a single video.
- **Download a whole list** of videos, keep going when one fails, and remember
  the failures so you can retry them later.

It works on Windows, macOS and Linux. Video extraction uses the open-source
[yt-dlp](https://github.com/yt-dlp/yt-dlp) project.

> Bottle Net Tool works with content that anyone can see **without signing
> in**. For Facebook content that needs a login, you can optionally use the
> session of a browser you are already signed in to (`--browser`). It never
> asks for or stores passwords, and it does not get around private accounts,
> access restrictions, CAPTCHAs, DRM or rate limits.

---

## Basic commands

```bash
bottle-net -h            # help (every command also has -h / --help)
bottle-net --version     # show the version
bottle-net doctor        # check that everything is installed and ready
```

## TikTok

```bash
bottle-net tiktok crawl @username                 # find the account's public videos
bottle-net tiktok crawl @username -o links.txt    # ...and save the links to links.txt
bottle-net tiktok download "VIDEO_URL"            # download one video
bottle-net tiktok download-list links.txt         # download every video in links.txt
```

## Facebook

```bash
bottle-net facebook crawl "PAGE_URL"
bottle-net facebook crawl "PAGE_URL" -o links.txt
bottle-net facebook download "VIDEO_URL"
bottle-net facebook download-list links.txt

# Content that requires you to be logged in: use your browser's session
bottle-net facebook crawl "PAGE_URL" --browser chrome
```

See [Facebook authentication](#facebook-authentication).

## Facebook authentication

Some Facebook pages/videos require you to be logged in.

Bottle Net Tool does **NOT** ask for or store your Facebook password.

Use an existing browser session:

```bash
bottle-net facebook crawl "<PAGE_URL>" --browser chrome
bottle-net facebook download "<VIDEO_URL>" --browser chrome
bottle-net facebook download-list links.txt --browser chrome
```

Supported browsers: `chrome`, `firefox`, `edge`, `brave`, `chromium`,
`opera`, `vivaldi`, `safari`. For another browser profile, use
`BROWSER:PROFILE`, for example `--browser "chrome:Profile 1"` or
`--browser "firefox:C:\path\to\profile"`.

Example:

```bash
bottle-net facebook crawl "https://www.facebook.com/NASA" --browser chrome
```

The selected browser must already be logged in to Facebook.

If the cookies cannot be read, close the browser completely and try again
(Chrome and Edge lock their cookie file while they run). Newer Chrome
versions on Windows protect their cookies in a way other programs cannot
always read; if that happens, log in to Facebook in Firefox and use
`--browser firefox`.

Security:

- Passwords are never requested.
- Cookies are read locally from the selected browser, and only the
  `facebook.com` cookies are used.
- Cookies/tokens are never printed, logged, saved or uploaded; they are sent
  only to facebook.com, exactly as your browser would send them.
- Authentication does not bypass private content or access restrictions:
  Bottle Net sees only what your own Facebook account can see.

Automated access while logged in is subject to Meta's terms. Keep the
default request delay, and use this only for content you are allowed to
download.

## Universal downloader

```bash
bottle-net download "VIDEO_URL"       # TikTok or Facebook, detected automatically
bottle-net download-list links.txt    # a list that mixes TikTok and Facebook links
```

## Video Publisher (GUI)

```bash
bottle-net gui                        # upload and schedule videos to YouTube and Facebook Pages
```

A desktop-style app in your browser for publishing your own videos with the
official YouTube Data API and Meta Graph API. See
[Video Publisher](#video-publisher) below.

> **Tip:** always put URLs in quotes. Characters such as `?` and `&` have a
> special meaning in most shells.

---

## Contents

- [Installation](#installation)
- [Facebook authentication](#facebook-authentication)
- [Video Publisher](#video-publisher)
- [A first session](#a-first-session)
- [Saving links: files, redirection and pipelines](#saving-links-files-redirection-and-pipelines)
- [Batch downloads and retrying failures](#batch-downloads-and-retrying-failures)
- [Stopping a download (Ctrl+C)](#stopping-a-download-ctrlc)
- [Where files are saved](#where-files-are-saved)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Responsible use](#responsible-use)
- [Command reference](#command-reference)
- [Development](#development)

## Installation

### Requirements

- **Python 3.11 or newer.** Check with `python --version`.
- An internet connection.
- *Optional:* [FFmpeg](https://ffmpeg.org/download.html). Without it, Bottle
  Net Tool downloads the best single file that already contains both video and
  audio, which works for almost all TikTok and Facebook videos. With FFmpeg on
  your `PATH`, it can also merge separate high-quality video and audio streams.

The Python packages it needs (`yt-dlp`, `rich`, `requests`) are installed
automatically.

### Install from the project folder

```bash
cd bottle-net
pip install .
```

### Install from GitHub

Clone the repository, then install the CLI from the checkout:

```bash
git clone https://github.com/Harry-Khit-Htoo/MediaCrawlerBottle-Net.git
cd MediaCrawlerBottle-Net
python -m pip install .
```

For a development checkout that uses your working tree directly:

```bash
python -m pip install -e ".[dev]"
```

After installation, run the command-line interface with `bottle-net`. If the
executable is not on your `PATH`, use `python -m bottle_net` instead.

### Install for development (editable, with test tools)

```bash
cd bottle-net
python -m venv .venv

# Activate the virtual environment:
#   Windows PowerShell:     .venv\Scripts\Activate.ps1
#   Windows Command Prompt: .venv\Scripts\activate.bat
#   macOS / Linux:          source .venv/bin/activate

pip install -e ".[dev]"
```

### Check the installation

```bash
bottle-net -h
bottle-net --version
bottle-net doctor
```

`bottle-net doctor` prints a report like this:

```text
Bottle Net Tool Doctor
────────────────────────────────

Python       ✓ 3.12.4
yt-dlp       ✓ installed (2026.08.19)
FFmpeg       ✓ installed
Config       ✓ valid (no config file; using defaults)
Downloads    ✓ writable (downloads)
Output       ✓ writable (output)

Platform support:
TikTok       ✓
Facebook     ✓

────────────────────────────────
System ready.
```

The doctor only checks your computer. It does not download anything or
contact TikTok or Facebook, and it does not create folders.

> **Note:** Bottle Net Tool is **not published on PyPI**, so
> `pip install bottle-net` does not work. Install it from the project folder
> as shown above.
>
> If `bottle-net` is "not recognized" or "command not found", Python's
> scripts folder is not on your `PATH`. Use `python -m bottle_net` instead,
> for example `python -m bottle_net -h`.

## Video Publisher

**Bottle Net - Video Publisher** uploads and schedules videos to **YouTube**
and **Facebook Pages**. It runs on your computer and opens in your web browser:

```bash
bottle-net gui
```

Keep the terminal window open while uploads are scheduled; press Ctrl+C to
stop. Pages: **Dashboard**, **Videos**, **Scheduler** (calendar),
**Upload Queue**, **History**, **Accounts** and **Settings**.

### What it does

- Add videos by drag & drop, from the file picker, from your Bottle Net
  `downloads` folder, or with **Download & Schedule** (uses the normal Bottle
  Net downloader).
- Title, description, tags (with `{filename}`, `{date}`, `{time}` templates),
  thumbnail, YouTube privacy and "made for kids".
- Publish now, or schedule several videos per day (default timezone
  `Asia/Bangkok`, changeable in Settings). Drag uploads between days in the
  calendar to move them.
- One video can go to YouTube and Facebook at once. Each platform is a
  separate upload: if YouTube succeeds and Facebook fails, YouTube stays
  published and only Facebook needs a **Retry**.
- Temporary errors (timeouts, connection resets, HTTP 429, server errors) are
  retried automatically: attempt 1 at the scheduled time, then +1, +5 and
  +15 minutes (configurable). Permanent errors (missing permissions,
  expired connection, invalid video or details) are not retried and are
  explained in plain language.
- Duplicate protection: a video that is already published (or scheduled) on a
  platform is not published there again unless you choose **Publish Again**.
- Schedules are stored on disk and survive restarts. Interrupted uploads
  resume. If Bottle Net was not running at a scheduled time, the upload is
  marked **Missed** and you choose Publish now, reschedule, or cancel (you can
  change this in Settings).
- Optional "Upload now and let YouTube/Facebook publish at this time" uses the
  platforms' own scheduling, so the video goes live even if Bottle Net is
  closed.

### Sign-in and security

- Bottle Net **never asks for your Google or Facebook password**. Clicking
  **Connect YouTube** / **Connect Facebook** opens Google's or Meta's own
  sign-in page (OAuth 2.0); Bottle Net only receives an access token.
- Tokens are stored in your operating system's credential store (Windows
  Credential Manager, macOS Keychain, Linux Secret Service), never in the job
  database, the browser, or the logs.
- The GUI listens on `127.0.0.1` only and is opened with a private link;
  other programs or websites cannot use it.
- Facebook publishing is for **Pages** you manage (not personal profiles).
- Bottle Net uses YouTube API Services. By connecting YouTube you agree to
  the [YouTube Terms of Service](https://www.youtube.com/t/terms). See the
  [Privacy Policy](docs/privacy.html), the [Terms of Service](docs/terms.html)
  and the [Google Privacy Policy](https://policies.google.com/privacy). You
  can remove access at any time with **Disconnect** or at
  <https://myaccount.google.com/permissions>.

### One-time setup: API credentials

Uploading uses your own (free) developer apps at Google and Meta. Put the
values in environment variables or in a `.env` file in the Bottle Net data
folder (Settings shows where; a template is in
[`publisher.env.example`](publisher.env.example)). Never commit this file.

**YouTube (Google Cloud Console)**

1. Create a project and enable the **YouTube Data API v3**.
2. Configure the OAuth consent screen and add yourself as a test user.
3. Create an OAuth client of type **Desktop app**.
4. Set `BOTTLE_NET_YOUTUBE_CLIENT_ID` and `BOTTLE_NET_YOUTUBE_CLIENT_SECRET`
   (or `BOTTLE_NET_YOUTUBE_CLIENT_SECRETS_FILE` pointing at the downloaded
   JSON file).

**Facebook (Meta for Developers)**

1. Create an app with **Facebook Login**.
2. Add `http://localhost:8765/oauth/facebook/callback` to **Valid OAuth
   Redirect URIs** (use your `--port` if you change it).
3. Request the permissions `pages_show_list`, `pages_read_engagement`,
   `pages_manage_posts` and `publish_video` (publishing for other people's
   Pages requires Meta's App Review).
4. Set `BOTTLE_NET_FACEBOOK_APP_ID` and `BOTTLE_NET_FACEBOOK_APP_SECRET`.
   Optionally pin a Graph API version with
   `BOTTLE_NET_FACEBOOK_GRAPH_VERSION` (for example `v23.0`).

Quotas and rules of both platforms apply (for example, the YouTube Data API
has a daily quota, and custom YouTube thumbnails need a verified channel).

### Only publish what you may publish

Downloaded videos usually belong to someone else. Publish only videos you
own or have permission to publish; Bottle Net asks you to confirm this for
downloaded videos.

### Optional terminal commands

```bash
bottle-net gui --port 8766 --no-browser   # another port; print the private link instead
bottle-net publisher jobs                 # list uploads
bottle-net publisher run                  # run the scheduler without the GUI
```

The publisher's data (database, imported videos, thumbnails, logs) lives in
`%LOCALAPPDATA%\bottle-net` on Windows, `~/Library/Application Support/bottle-net`
on macOS and `~/.local/share/bottle-net` on Linux (`BOTTLE_NET_DATA_DIR`
overrides it). Technical error details go to `logs/publisher.log` there, with
tokens removed.

## A first session

```bash
# 1. Is everything ready?
bottle-net doctor

# 2. Collect the links of a public TikTok account
bottle-net tiktok crawl @username -o links.txt

# 3. Download them all
bottle-net tiktok download-list links.txt
```

What a crawl looks like:

```text
╔════════════════════════════════════════════════╗
║                BOTTLE NET TOOL                 ║
║                 TikTok Crawler                 ║
╚════════════════════════════════════════════════╝

Target: @username

[+] Starting crawler
[+] Discovering public videos

Progress: ████████████████████████████████ 100% 127 found

Videos found: 127
Duplicates:   3

[✓] Crawl completed

Output:
  links.txt
```

What a download looks like:

```text
Title: Example Video
File:  Example_Video_7123456789012345678.mp4
Size:  12.4 MB

Downloading
████████████████████████████████ 100% 12.4 MB

Speed: 4.8 MB/s (average)

[✓] Download completed

Saved:
  downloads/tiktok/Example_Video_7123456789012345678.mp4
```

## Saving links: files, redirection and pipelines

Bottle Net Tool keeps **data** and **messages** apart:

```text
stdout (standard output) = URLs / data
stderr (standard error)  = progress bars and status messages
```

Shell redirection (`>`) only captures stdout, so it always produces a clean
file that contains nothing but URLs, one per line. The progress and status
messages still appear in your terminal.

| You run | The links go to |
| --- | --- |
| `bottle-net tiktok crawl @username` in a terminal | A new file, `output/tiktok_username_links.txt`. An existing file is **never overwritten**: `_links_2.txt`, `_links_3.txt`, ... are used instead. |
| `... crawl @username -o links.txt` | `links.txt` (replaced if it already exists; the tool tells you) |
| `... crawl @username > links.txt` | `links.txt`, through standard output |
| `... crawl @username --quiet > links.txt` | `links.txt`, and no status messages are shown at all |
| `... crawl @username \| sort -u > links.txt` | The next program in the pipeline |

```bash
bottle-net tiktok crawl @username > links.txt
bottle-net tiktok crawl @username --quiet > links.txt
bottle-net tiktok crawl @username | sort -u > links.txt
```

`sort -u` exists on macOS, Linux and Git Bash for Windows. In PowerShell,
use `... | Sort-Object -Unique | Set-Content links.txt` instead. Bottle Net Tool
already removes duplicates itself, so sorting is optional.

To keep the status messages in a log file as well, redirect stderr:
`bottle-net tiktok crawl @username > links.txt 2> crawl.log`.

### Reading links from standard input

Use `-` instead of a filename to read the links from standard input:

```bash
cat links.txt | bottle-net tiktok download-list -
bottle-net tiktok crawl @username --quiet | bottle-net tiktok download-list -
```

```powershell
Get-Content links.txt | bottle-net tiktok download-list -
```

## Batch downloads and retrying failures

A link list is a plain text file with **one URL per line**. Blank lines,
lines starting with `#`, and duplicate URLs are ignored.

```text
# My favourite videos
https://www.tiktok.com/@example/video/1234567890
https://www.tiktok.com/@example/video/2345678901
```

`download-list` checks every URL, downloads the videos into one folder,
skips videos that are already there, and **keeps going when a video fails**:

```text
Total: 127

[001/127] https://www.tiktok.com/@username/video/...
  [✓] Saved Example_Video_7123456789012345678.mp4
...
      [████████████████████████████████] 100% 127/127 videos

Successful:  123
Failed:        4
Skipped:       0

Failed URLs:
  output/failed_tiktok.txt

[!] 4 download(s) failed.

Retry failed downloads with:

  bottle-net tiktok download-list "output/failed_tiktok.txt" \
    --output-dir "downloads/tiktok/username"
```

The retry command sends the videos back to the **same folder** as the first
run. The failed-URL file also records that folder, so running
`bottle-net tiktok download-list output/failed_tiktok.txt` without any
options also saves into the original folder, never into a new one. On
Windows the command is printed on a single line, because the `\` line
continuation only works in macOS/Linux shells.

Network errors are retried automatically, with increasing pauses. If the
platform starts rate limiting, Bottle Net Tool slows down and eventually
stops instead of pushing harder. The URLs it did not try are saved so you can
continue later.

## Stopping a download (Ctrl+C)

Press **Ctrl+C** at any time. You get a short summary instead of an error
dump:

```text
[!] Download interrupted by user.

Completed:    12
Remaining:    43

Partial files have been handled safely: unfinished downloads are kept as
".part" files, are never counted as completed, and resume on the next run.
```

The URLs that were not finished are saved to the failed-URL file, and the
summary shows the command to resume. The program exits with code `130`.

## Where files are saved

Folders are created automatically when they are needed.

```text
downloads/
├── tiktok/
│   ├── <video>.mp4                 ← single downloads
│   └── username/                   ← batch from tiktok_username_links.txt
└── facebook/
    └── examplepage/
output/
├── tiktok_username_links.txt       ← crawl results
├── failed_tiktok.txt               ← failed URLs from the last TikTok batch
├── failed_facebook.txt
└── failed_downloads.txt            ← from `bottle-net download-list`
```

- **Batch folders** are named after the list file, without the platform
  prefix and the `_links` suffix: `tiktok_username_links.txt` becomes
  `username/`. For links read from standard input, the folder is named after
  the TikTok account (or `stdin`). Use `--name NAME` or `--output-dir DIR` to
  choose the folder yourself.
- **Video files** are named `<title>_<video id>.<ext>`. The video ID keeps
  names unique and lets the tool recognise videos it already has.
- **Filenames are safe on every system.** The characters `< > : " / \ | ? *`
  never appear (`/` and `\` become `-`, the others are removed), spaces become
  `_`, Windows reserved names such as `CON` or `NUL` are changed, and names are
  shortened so that neither the name nor the full path exceeds the limits of
  Windows, macOS or Linux. Titles in any language (Burmese, Thai, emoji ...)
  are kept. For example, `My Video: Part 1/2?.mp4` becomes
  `My_Video_Part_1-2.mp4`.

## Configuration

No configuration is needed. To change the defaults, create a `config.toml`
file (a commented template is included as
[`config.example.toml`](config.example.toml)):

```toml
download_directory = "downloads"   # where videos are saved
output_directory = "output"        # where link lists and failed-URL files go
max_retries = 3                    # retries after network errors / rate limits (0-10)
timeout = 30                       # network timeout in seconds (1-600)
request_delay = 1.0                # pause between requests, in seconds (0-60)
```

Bottle Net Tool looks for the configuration file in this order:

1. The file given with `--config FILE`
2. The file named by the `BOTTLE_NET_CONFIG` environment variable
3. `config.toml` in the current folder
4. Your user configuration folder (Windows: `%APPDATA%\bottle-net\config.toml`;
   macOS/Linux: `~/.config/bottle-net/config.toml`)

`bottle-net doctor` tells you whether your configuration is valid.

## Troubleshooting

Start with `bottle-net doctor`. It finds most setup problems.

| Message | What it means and what to do |
| --- | --- |
| `[!] URL is invalid.` | The text is not a web address, or it is the wrong kind of link (for example a profile link passed to `download`). The message explains which command to use. |
| `[!] Unsupported platform.` | Only TikTok and Facebook are supported. |
| `[!] Video unavailable.` | The video was deleted, made private, or is restricted in your region. |
| `[!] Content is private.` / `[!] Login is required.` | The content is not public. For Facebook content your own account can see, log in to facebook.com in your browser and add `--browser chrome` (or firefox, edge, ...). Content your account cannot see stays inaccessible. |
| `[!] Could not use your browser's Facebook login.` | The browser is not logged in to Facebook, is still running (close it), or its cookies cannot be read. See [Facebook authentication](#facebook-authentication). |
| `[!] Rate limit detected.` | The platform wants you to slow down. Wait a while (for example 15–60 minutes), or raise `request_delay` in `config.toml`. |
| `[!] Network timeout.` / `[!] Network error.` | Check your internet connection. These errors are retried automatically. |
| `[!] Access blocked by the platform.` | The platform asked for a CAPTCHA or blocked automated access from your network. Bottle Net Tool does not bypass this; try again later. |
| `[!] Unable to extract this video.` | The website may have changed, or the video may no longer be public. Try updating yt-dlp: `pip install --upgrade yt-dlp`. |
| `TikTok did not return the profile data. Retrying...` | TikTok sometimes answers anonymous profile requests without data. The tool asks again automatically; if the crawl still fails, run it again later or raise `max_retries`. |
| `[!] Note: Facebook may expose only the first batch...` | Expected. Without signing in, Facebook shows only the first batch of a Page's videos, so the list may be incomplete. Bottle Net Tool does not use login credentials or private APIs. |
| The redirected file contains status text | It cannot, because status goes to stderr. If you used `2>&1` or `*>`, you merged stderr into the file; use plain `>`. |
| `bottle-net` is not recognized | Use `python -m bottle_net ...`, or add Python's `Scripts` folder to your `PATH`. |

For technical details on any problem, add `--debug`:

```bash
bottle-net --debug tiktok download "VIDEO_URL"
```

Debug output never contains passwords, cookies, authorization headers or
tokens. Bottle Net Tool does not use any, and anything that looks like one
(for example a signed media URL) is replaced with `[REDACTED]`.

## Responsible use

- Only download content you have permission to download, for example your own
  videos, or content whose owner allows it.
- Respect copyright. Downloading a video does not give you the right to
  re-upload, share or sell it.
- Respect the platforms' terms of service. TikTok and Facebook restrict
  automated access and downloading; you are responsible for how you use the
  tool.
- With `--browser`, Bottle Net uses your own Facebook session: only content
  your account is allowed to see, never other people's private content.
- Bottle Net Tool only processes publicly accessible content (or, with
  `--browser`, content visible to your own account). It does **not**
  access private accounts, bypass logins, solve or bypass CAPTCHAs, remove
  DRM, or circumvent any other access control. When a platform blocks access,
  the tool stops and tells you why.

This software is provided "as is", without warranty. See [LICENSE](LICENSE).

## Command reference

```text
bottle-net tiktok   crawl <ACCOUNT> [OPTIONS]
bottle-net tiktok   download <URL> [OPTIONS]
bottle-net tiktok   download-list <FILE | -> [OPTIONS]

bottle-net facebook crawl <PAGE_OR_PROFILE> [OPTIONS]
bottle-net facebook download <URL> [OPTIONS]
bottle-net facebook download-list <FILE | -> [OPTIONS]

bottle-net download <URL> [OPTIONS]
bottle-net download-list <FILE | -> [OPTIONS]
bottle-net doctor
```

| Option | Commands | Meaning |
| --- | --- | --- |
| `-h`, `--help` | all | Show help for that command |
| `-v`, `--version` | top level | Show the version |
| `-o FILE`, `--output FILE` | `crawl` | Save the links to `FILE` (`-o -` means standard output) |
| `-q`, `--quiet` | all | `crawl`: print only the URLs. Other commands: show only errors |
| `--format FORMAT` | `crawl` | Output format: `txt` (the default), one URL per line |
| `--limit N` | `crawl` | Stop after finding `N` videos |
| `-d DIR`, `--output-dir DIR` | `download`, `download-list` | Save videos in `DIR` (`--dir` also works) |
| `--name NAME` | `download-list` | Sub-folder name for the batch |
| `--failed-file FILE` | `download-list` | Where to write failed URLs |
| `--browser BROWSER` | `facebook crawl`, `download`, `download-list` | Use the Facebook login of a browser you are signed in to |
| `--config FILE` | all | Use a specific configuration file |
| `--debug` | all | Show detailed diagnostic output |
| `--no-color` | all | Disable colors (the `NO_COLOR` environment variable also works) |

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The operation failed, or at least one video in a batch failed |
| `2` | Invalid command-line usage |
| `130` | Interrupted with Ctrl+C |

## Development

### Project layout

```text
bottle-net/
├── bottle_net/
│   ├── __init__.py          Version and author (the single source of the version)
│   ├── __main__.py          `python -m bottle_net`
│   ├── cli.py               Argument parsing, help text, dispatch
│   ├── commands.py          Command implementations (presentation layer)
│   ├── doctor.py            `bottle-net doctor`
│   ├── config.py            Optional TOML configuration
│   ├── errors.py            Error types with user-facing headlines
│   ├── ytdlp.py             yt-dlp options and error classification
│   ├── crawlers/            TikTok (yt-dlp) and Facebook (public HTML) crawlers
│   ├── downloaders/         Retries, single and batch downloads, auto-detection
│   └── utils/
│       ├── console.py       Human-readable output (stderr)
│       ├── output.py        Machine-readable output (stdout) and formats
│       ├── progress.py      Progress bars
│       ├── filenames.py     Central filename sanitising
│       ├── files.py         Link lists, failed-URL files, download lookup
│       ├── logger.py        Logging and secret redaction
│       └── urls.py          URL validation, normalisation, de-duplication
├── docs/                    Website: homepage, Privacy Policy, Terms (GitHub Pages)
├── verification/           Google OAuth verification checklist and texts
├── tests/                   pytest suite (no real network access)
├── config.example.toml
├── pyproject.toml
├── requirements.txt / requirements-dev.txt
├── README.md
└── LICENSE
```

Crawlers and downloaders never print anything. They report progress through
callbacks and raise the typed errors from `errors.py`, and the CLI layer
decides how to show them. Human-readable output always goes to stderr; only
data goes to stdout.

### Tests and checks

```bash
pip install -e ".[dev]"
pytest                       # the full test suite (GUI view tests need Node.js)
ruff check bottle_net tests  # lint
mypy bottle_net              # type check
```

The tests replace yt-dlp and HTTP with in-memory fakes, so they are fast and
never contact TikTok or Facebook. `tests/test_interrupt.py` runs the real CLI
in a separate process and interrupts it with a real SIGINT, the signal Ctrl+C
sends.

### Releasing a new version

Change `__version__` in `bottle_net/__init__.py`. `pyproject.toml` reads the
version from there, and `bottle-net --version` shows it.

### Keeping extraction working

Platforms change their websites often. Most extraction failures are fixed by
updating yt-dlp:

```bash
pip install --upgrade yt-dlp
```
