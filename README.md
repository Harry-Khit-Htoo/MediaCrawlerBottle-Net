# Bottle Net Tool

```text
╔════════════════════════════════════════════════╗
║                BOTTLE NET TOOL                 ║
║       Public Video Crawler & Downloader        ║
║                                                ║
║             Author: Aung Khit Htoo             ║
╚════════════════════════════════════════════════╝
```

**Bottle Net Tool** (`bottle-net`) is a command-line tool for saving **publicly
accessible** TikTok and Facebook videos to your computer.

It can:

- **Crawl** a public TikTok account or Facebook Page and collect the URLs of its
  videos into a text file.
- **Download** a single video from its URL.
- **Batch-download** every video in a URL list, continuing past failures and
  recording anything that failed so you can retry it later.

It uses the well-established open-source [yt-dlp](https://github.com/yt-dlp/yt-dlp)
library for video extraction and [Rich](https://github.com/Textualize/rich) for
the terminal interface.

> **Bottle Net Tool only works with content that anyone can see without signing
> in.** It never logs in and never uses cookies or passwords. It does not work
> around private accounts, login walls, CAPTCHAs, DRM or rate limits. When a
> platform blocks access, the tool stops and tells you why.

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Saving links, redirection and pipelines](#saving-links-redirection-and-pipelines)
- [TikTok examples](#tiktok-examples)
- [Facebook examples](#facebook-examples)
- [Batch downloading](#batch-downloading)
- [Output directories](#output-directories)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Legal and ethical use](#legal-and-ethical-use)
- [Development](#development)

---

## Features

- Simple subcommands: `bottle-net <platform> <command>`
- Automatic platform detection: `bottle-net download <URL>` and
  `bottle-net download-list <FILE>` (mixed TikTok/Facebook lists)
- Unix-friendly: URL lists go to **stdout** and status messages to
  **stderr**, so `>`, `|` and reading from stdin (`-`) all work cleanly
- `-q` / `--quiet` mode for scripts and pipelines
- Progress bars showing percentage, file size, download speed and time remaining
- A live counter of videos found while crawling
- Duplicate URLs are removed automatically, including the same video written
  in different URL forms
- Videos that are already downloaded are skipped, and interrupted downloads
  resume where they stopped
- Batch downloads continue when one video fails, and failed URLs are saved to a
  file you can retry directly
- Transient network errors are retried with exponential backoff
- Rate limits are respected: the tool slows down, and it stops if the platform
  keeps refusing
- Clear error messages with a reason and a hint for what to do next
- `-h` / `--help` on every command
- Optional `config.toml`; no configuration is required
- Works on Windows, macOS and Linux

## Requirements

- **Python 3.11 or newer.** Check with `python --version`.
- An internet connection.
- *Optional:* [FFmpeg](https://ffmpeg.org/download.html). Without FFmpeg, the
  tool downloads the best single file that already contains both video and
  audio, which is fine for almost all TikTok and Facebook videos. With FFmpeg
  installed and on your `PATH`, it can also merge separate high-quality video
  and audio streams.

The Python packages it needs (`yt-dlp`, `rich`, `requests`) are installed
automatically.

## Installation

### From a local copy or Git repository (recommended)

```bash
git clone <repository-url>
cd bottle-net
pip install .
```

### Development (editable) install

```bash
git clone <repository-url>
cd bottle-net
python -m venv .venv

# Activate the virtual environment:
#   Windows (PowerShell):  .venv\Scripts\Activate.ps1
#   macOS / Linux:         source .venv/bin/activate

pip install -e ".[dev]"
```

### From PyPI

Once the package is published to PyPI, you can install it with:

```bash
pip install bottle-net
```

### Check that it works

```bash
bottle-net -h
bottle-net --version
```

> If your shell says `bottle-net: command not found`, the Python scripts folder
> is not on your `PATH`. You can always run the tool as `python -m bottle_net`
> instead, for example `python -m bottle_net -h`.

## Quick start

```bash
# 1. Collect the video URLs of a public TikTok account into a file
bottle-net tiktok crawl @username -o links.txt

# 2. Download every video in that file
bottle-net tiktok download-list links.txt

# Or do both in one pipeline
bottle-net tiktok crawl @username -q | bottle-net tiktok download-list -

# Or download a single video (put URLs in quotes)
bottle-net download "https://www.tiktok.com/@username/video/1234567890"
```

## Commands

```text
bottle-net tiktok   crawl <ACCOUNT>            Find public video URLs on a TikTok account
bottle-net tiktok   download <URL>             Download one TikTok video
bottle-net tiktok   download-list <FILE | ->   Download all videos in a URL list

bottle-net facebook crawl <PAGE_OR_PROFILE>    Find public video URLs on a Facebook Page
bottle-net facebook download <URL>             Download one Facebook video
bottle-net facebook download-list <FILE | ->   Download all videos in a URL list

bottle-net download <URL>                      Download one video (platform auto-detected)
bottle-net download-list <FILE | ->            Download a list (platform detected per URL)
```

`-` as the FILE means "read the URLs from standard input".

### Options

| Option | Commands | Meaning |
| --- | --- | --- |
| `-h`, `--help` | all | Show help for that command |
| `-v`, `--version` | top level | Show the version |
| `-o FILE`, `--output FILE` | `crawl` | Save the links to `FILE` (`-o -` = standard output) |
| `-q`, `--quiet` | all | `crawl`: print only the URLs. Others: show only errors |
| `--format FORMAT` | `crawl` | Output format; currently `txt` (the default), one URL per line |
| `--limit N` | `crawl` | Stop after finding `N` videos |
| `-d DIR`, `--dir DIR` | `download`, `download-list` | Save videos in `DIR` |
| `--name NAME` | `download-list` | Sub-folder name for the batch (default: from the list's filename) |
| `--failed-file FILE` | `download-list` | Where to write failed URLs |
| `--config FILE` | all | Use a specific configuration file |
| `--debug` | all | Show detailed diagnostic output (useful when reporting problems) |
| `--no-color` | all | Disable colored output (the `NO_COLOR` environment variable also works) |

> **Tip:** Always put URLs in quotes. Characters such as `?` and `&` have
> special meanings in most shells.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | The operation failed, or at least one video in a batch failed |
| `2` | Invalid command-line usage |
| `130` | Interrupted with Ctrl+C |

## Saving links, redirection and pipelines

Bottle Net Tool follows the Unix convention of keeping **data** and
**messages** apart:

- **stdout** (standard output) carries only data: the crawled video URLs,
  one per line.
- **stderr** (standard error) carries everything meant for you: the banner,
  status lines, progress bars, warnings and errors.

That means redirected or piped output contains URLs and nothing else.

### Where `crawl` puts the links

| You run | The links go to |
| --- | --- |
| `bottle-net tiktok crawl @username` in a terminal | A new file, `output/tiktok_username_links.txt`. An existing file is **never overwritten**: `_links_2.txt`, `_links_3.txt`, ... are used instead. |
| `... crawl @username -o links.txt` (or `--output`) | `links.txt` (replaced if it already exists; the tool tells you) |
| `... crawl @username > links.txt` | `links.txt`, through standard output. Status still shows in the terminal. |
| `... crawl @username \| sort -u` | The next program in the pipeline |
| `... crawl @username -q` | Standard output, with no status messages at all |
| `... crawl @username -o -` | Standard output (explicitly) |

```bash
# Choose the file name
bottle-net tiktok crawl @username -o example-links.txt
bottle-net facebook crawl https://www.facebook.com/examplepage --output facebook-links.txt

# Shell redirection: the file contains only URLs
bottle-net tiktok crawl @username > example-links.txt

# Quiet, machine-readable output for scripts
bottle-net tiktok crawl @username -q > example-links.txt

# Pipelines
bottle-net tiktok crawl @username | sort -u > unique-links.txt
bottle-net tiktok crawl @username -q | head -20

# Choose the format explicitly (txt is the default and currently the only one)
bottle-net tiktok crawl @username --output links.txt --format txt
```

While the URLs flow to the file or pipe, the terminal still shows the status
on stderr:

```text
[+] Crawling @username...
✓ Crawl finished ████████████████████████████████ 100% 127 found 0:00:41
[+] Videos found: 127
[✓] Crawl completed
[i] 127 URL(s) written to standard output.
```

To also hide these messages, add `-q`. To save them to a log, redirect stderr
as well: `bottle-net tiktok crawl @username > links.txt 2> crawl.log`.

### Reading links from standard input

Use `-` instead of a filename to make `download-list` read the URLs from
standard input:

```bash
cat unique-links.txt | bottle-net tiktok download-list -
bottle-net tiktok crawl @username -q | bottle-net tiktok download-list -

# Mixed TikTok and Facebook links: the universal command detects each URL
cat links.txt | bottle-net download-list -
```

```powershell
# PowerShell
Get-Content links.txt | bottle-net tiktok download-list -
```

Videos read from standard input are saved in a folder named after the TikTok
account when all links belong to one account (`downloads/tiktok/username/`),
otherwise in `.../stdin/`. Use `--name NAME` or `-d DIR` to choose.

### Complete example

```bash
bottle-net tiktok crawl @username                  # crawl and save automatically
bottle-net tiktok crawl @username -o example-links.txt
bottle-net tiktok crawl @username > example-links.txt
bottle-net tiktok crawl @username -q > example-links.txt
bottle-net tiktok download-list example-links.txt
bottle-net tiktok crawl @username | sort -u > unique-links.txt
cat unique-links.txt | bottle-net tiktok download-list -
```

> **Windows notes:** URL lists are written with Unix (`\n`) line endings and
> UTF-8 encoding. Files created by Windows PowerShell 5.1's `>` operator
> (UTF-16) and PowerShell 7 (UTF-8) are both read correctly, as are files
> with Windows (`\r\n`) line endings.

## TikTok examples

```bash
# Crawl an account. Any of these forms work:
bottle-net tiktok crawl @username
bottle-net tiktok crawl username
bottle-net tiktok crawl https://www.tiktok.com/@username

# Only collect the 50 most recent videos, saved to a custom file
bottle-net tiktok crawl @username --limit 50 -o my-list.txt

# Print the links instead of saving them
bottle-net tiktok crawl @username -q

# Download a single video
bottle-net tiktok download "https://www.tiktok.com/@username/video/1234567890"

# Short share links work too
bottle-net tiktok download "https://vm.tiktok.com/ZMabcdef/"

# Download everything in a list
bottle-net tiktok download-list output/tiktok_username_links.txt
```

Example crawl output (in a terminal, without `-o`):

```text
╭────────────────────────────────────────────────╮
│                BOTTLE NET TOOL                 │
│              TikTok Video Crawler              │
╰────────────────────────────────────────────────╯

Target: https://www.tiktok.com/@example

[+] Crawling @example...

✓ Crawl finished ████████████████████████████████ 100% 127 found 0:00:41

[+] Videos found: 127

[✓] Crawl completed

Saved:
  output/tiktok_example_links.txt

[i] Next: bottle-net tiktok download-list "output/tiktok_example_links.txt"
```

The crawler follows TikTok's own pagination until no more videos are
available (or until `--limit`). Photo slideshow posts are skipped because they
are not videos.

## Facebook examples

```bash
# Crawl a public Page
bottle-net facebook crawl https://www.facebook.com/examplepage
bottle-net facebook crawl examplepage

# Numeric profile URLs are supported
bottle-net facebook crawl "https://www.facebook.com/profile.php?id=100000000000000"

# Download a single video or reel
bottle-net facebook download "https://www.facebook.com/watch/?v=1234567890"
bottle-net facebook download "https://www.facebook.com/reel/1234567890"
bottle-net facebook download "https://fb.watch/abcDEF/"

# Download everything in a list
bottle-net facebook download-list output/facebook_examplepage_links.txt
```

**About Facebook crawling:** Facebook includes the first batch of a Page's
videos and reels in the public page that anyone can open without signing in.
Bottle Net Tool reads the Page's public **Videos** and **Reels** tabs and
collects those links. Loading older videos needs a signed-in session or
Facebook's private internal API. Bottle Net Tool uses neither, so for Pages
with many videos the list may be partial, and the tool tells you when that
happens. Individual video URLs you already have can always be downloaded with
`facebook download` or `download-list` (as long as they are public).

## Batch downloading

A URL list is a plain text file with **one URL per line** (or the same
text piped in on standard input with `-`):

```text
# Lines starting with "#" are comments and are ignored.
https://www.tiktok.com/@example/video/1234567890
https://www.tiktok.com/@example/video/2345678901

https://www.tiktok.com/@example/video/3456789012
```

When `download-list` runs, it:

1. Reads the file (or standard input), ignoring blank lines, comments and
   duplicate URLs.
2. Validates every URL. Invalid ones are reported as failures without touching
   the network.
3. Downloads each video into a single folder, showing a progress bar for the
   current file and for the whole batch.
4. Skips videos that are already in the folder.
5. Keeps going if a video fails, for example because it was deleted, is
   private, or hit a network error.
6. Prints a summary and saves the failed URLs to `output/failed_<platform>.txt`
   (`output/failed_downloads.txt` for the universal `bottle-net download-list`).

```text
Download Summary
────────────────────────────────
Successful:   94
Failed:        6
Skipped:       0
────────────────────────────────

Failed URLs:
  output/failed_tiktok.txt
[i] Retry them with: bottle-net tiktok download-list "output/failed_tiktok.txt" -d "downloads/tiktok/example"
```

The failed-URL file is itself a valid URL list. Each URL is preceded by a
comment explaining why it failed, so you can retry it using the exact command
printed in the summary and in the file's header.

Press **Ctrl+C** at any time to stop. URLs that were not finished are written
to the failed-URL file, and partially downloaded videos resume the next time
you run the command.

## Output directories

Folders are created automatically when they are needed.

```text
downloads/
├── tiktok/
│   ├── <video>.mp4               ← single downloads
│   └── example/                  ← batch from tiktok_example_links.txt
│       └── <video>.mp4
└── facebook/
    ├── <video>.mp4
    └── examplepage/
        └── <video>.mp4
output/
├── tiktok_example_links.txt            ← crawl results
├── tiktok_example_links_2.txt          ← a later crawl (never overwrites)
├── facebook_examplepage_links.txt
├── failed_tiktok.txt                   ← failed URLs from the last batch
├── failed_facebook.txt
└── failed_downloads.txt                ← from `bottle-net download-list`
```

- **Video filenames** look like `<title>_<video id>.mp4`, for example
  `Example_video_7123456789012345678.mp4`. The video ID keeps names unique and
  lets the tool recognise videos it has already downloaded. Titles in any
  language (Burmese, Thai, emoji, and so on) are kept, and only characters that
  are illegal in filenames are removed. Long titles are shortened to keep paths
  within Windows' 260-character limit.
- **Batch folder names** come from the list's filename, without the platform
  prefix and the `_links` suffix: `tiktok_example_links.txt` and
  `tiktok_example_links_2.txt` both become `example/`. Use `--name NAME` or
  `-d DIR` to choose a different folder.

## Configuration

No configuration is needed. To change the defaults, create a `config.toml`
file. A commented template is included as
[`config.example.toml`](config.example.toml).

```toml
download_directory = "downloads"   # where videos are saved
output_directory = "output"        # where URL lists and failed-URL files go
max_retries = 3                    # retries after network errors / rate limits (0-10)
timeout = 30                       # network timeout in seconds (1-600)
request_delay = 1.0                # pause between requests, in seconds (0-60)
```

Bottle Net Tool looks for the configuration file in this order:

1. The file given with `--config FILE`
2. The file named by the `BOTTLE_NET_CONFIG` environment variable
3. `config.toml` in the current folder
4. Your user configuration folder:
   - Windows: `%APPDATA%\bottle-net\config.toml`
   - macOS / Linux: `~/.config/bottle-net/config.toml`

Settings can also be placed inside a `[bottle_net]` table. Invalid values
produce a clear error that names the setting.

## Troubleshooting

| Message | What it means / what to do |
| --- | --- |
| `Unsupported or unknown URL` | The URL is not from TikTok or Facebook. Check that you copied the full URL. |
| `This is a TikTok profile URL, not a video URL` | Use `bottle-net tiktok crawl` for accounts and `download` for single videos. |
| `Video is unavailable` | The video was deleted, made private, or is not available in your region. |
| `Content is private` / `Login required` | The content is not public. Bottle Net Tool does not sign in, so it cannot download it. |
| `Rate limited by platform` | You have sent too many requests. Wait a while (for example 15–60 minutes) and try again, or raise `request_delay` in `config.toml`. |
| `Access blocked by platform` | The platform asked for a CAPTCHA or blocked automated access from your network. Bottle Net Tool will not work around this. Try again later. |
| `Could not read the video ... changed its public interface` | TikTok or Facebook changed their website. Update the extractor: `pip install --upgrade yt-dlp`. |
| `TikTok did not return the profile data - retrying` | TikTok sometimes answers anonymous profile requests without the account data. The tool asks again with increasing pauses (up to `max_retries` times, default 3), which almost always works. If the crawl still fails, run it again or raise `max_retries` in `config.toml`. |
| Redirected file contains status text | It cannot: status goes to stderr. If you used `2>&1` or `*>`, you merged stderr into the file; use plain `>` instead. |
| `Facebook only shows the first batch of videos...` | This is expected. See [About Facebook crawling](#facebook-examples). |
| `Could not write the video file to disk` | Check free disk space and folder permissions, or choose another folder with `-d`. |
| Garbled boxes or symbols on Windows | Use Windows Terminal, or run with `--no-color`. |
| `bottle-net: command not found` | Use `python -m bottle_net ...`, or add Python's `Scripts` folder to your `PATH`. |

For detailed diagnostics, add `--debug` to any command:

```bash
bottle-net --debug tiktok download "https://www.tiktok.com/@user/video/123"
```

## Legal and ethical use

Bottle Net Tool is intended for **personal and lawful use** only, such as
keeping copies of your own content, archiving material you have permission to
save, research, or journalism within the law.

- **Respect copyright.** Most videos belong to their creators. Downloading
  does not give you the right to re-upload, redistribute or monetise someone
  else's work.
- **Respect the platforms' Terms of Service.** TikTok's and Facebook's terms
  restrict automated collection and downloading. You are responsible for making
  sure your use is permitted.
- **Respect privacy.** Do not use the tool to track, profile or harass people.
- **Respect the platforms' limits.** The tool deliberately paces its requests,
  backs off when rate limited, and never bypasses logins, private accounts,
  CAPTCHAs, DRM or other access controls.

The author provides this software "as is", without warranty, and is not
responsible for how it is used. See [LICENSE](LICENSE).

## Development

### Project layout

```text
bottle-net/
├── bottle_net/
│   ├── __init__.py          Version and author
│   ├── __main__.py          `python -m bottle_net`
│   ├── cli.py               Argument parsing, help text, dispatch
│   ├── commands.py          Command implementations (presentation layer)
│   ├── config.py            Optional TOML configuration
│   ├── errors.py            Exception hierarchy
│   ├── ytdlp.py             yt-dlp options and error classification
│   ├── crawlers/
│   │   ├── base.py          CrawlResult
│   │   ├── tiktok.py        TikTok account crawler (yt-dlp, flat mode)
│   │   └── facebook.py      Facebook Page crawler (public HTML)
│   ├── downloaders/
│   │   ├── auto.py          Per-URL platform detection for universal commands
│   │   ├── common.py        Retries, single download, batch runner
│   │   ├── tiktok.py        TikTok downloader
│   │   └── facebook.py      Facebook downloader
│   └── utils/
│       ├── console.py       Banner, headers, status lines, summaries
│       ├── files.py         URL lists, failed-URL files, filenames
│       ├── logger.py        Logging setup (--debug)
│       ├── output.py        stdout data writers and output formats
│       ├── progress.py      Progress bars
│       └── urls.py          URL validation, normalization, de-duplication
├── tests/                   pytest suite (no real network access)
├── downloads/               Default video folder
├── output/                  Default URL-list folder
├── config.example.toml
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── README.md
└── LICENSE
```

The CLI layer (`cli.py`, `commands.py`) handles only parsing and presentation.
Crawlers and downloaders never print anything. They report progress through
callbacks and raise typed errors from `errors.py`.

**Output streams:** human-readable output (`utils/console.py`,
`utils/progress.py`) always goes to **stderr**. Only data goes to **stdout**,
through a writer from `utils/output.py`. New output formats (for example
`json` or `csv`) are added by registering them in `OUTPUT_FORMATS`.

### Running the tests

```bash
pip install -e ".[dev]"
pytest
```

The tests replace yt-dlp and HTTP with in-memory fakes (see
`tests/conftest.py`), so they are fast, deterministic and never contact TikTok
or Facebook. They cover argument parsing, help output, URL validation,
de-duplication, file reading and writing (including UTF-16 and stdin),
configuration, error classification, retry logic, batch behaviour,
interruption, stdout/stderr separation, quiet mode, closed pipes and every CLI
command.

### Adding a platform

1. Add the platform to `Platform` in `utils/urls.py`, with URL validation.
2. Add a `VideoDownloader` subclass in `downloaders/` and register it in
   `downloaders/__init__.py`.
3. Add a crawler in `crawlers/` and wire up its subcommands in `cli.py`.

### Keeping extraction working

Platforms change their websites often. Most breakages are fixed by updating
yt-dlp:

```bash
pip install --upgrade yt-dlp
```

---

**Bottle Net Tool** · Author: **Aung Khit Htoo** · MIT License
