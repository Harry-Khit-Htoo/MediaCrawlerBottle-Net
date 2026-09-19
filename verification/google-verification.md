# Google OAuth verification kit

Steps and ready-to-paste text for moving Bottle Net's YouTube sign-in from
**Testing** to a verified **In production** app that any Google account can
use.

Author / developer: **Aung Khit Htoo**

The website in [`docs/`](../docs/) is what Google's reviewers check:

| Page | File | URL (GitHub Pages, before a custom domain) |
| --- | --- | --- |
| Homepage | `docs/index.html` | `https://harry-khit-htoo.github.io/MediaCrawlerBottle-Net/` |
| Privacy Policy | `docs/privacy.html` | `https://harry-khit-htoo.github.io/MediaCrawlerBottle-Net/privacy.html` |
| Terms of Service | `docs/terms.html` | `https://harry-khit-htoo.github.io/MediaCrawlerBottle-Net/terms.html` |

This file is kept outside `docs/` on purpose, so it is not published on the
website.

## 1. Before you submit

- [ ] **Set the support email.** Replace every `REPLACE_WITH_SUPPORT_EMAIL`
      in `docs/*.html` (3 files) with the address users and reviewers should
      write to, ideally one on your domain:

      ```bash
      sed -i 's/REPLACE_WITH_SUPPORT_EMAIL/support@YOUR-DOMAIN/g' docs/*.html
      ```

      Use the same address as the **User support email** on the Branding page.
- [ ] **Get a domain** (for example `bottlenet.app`). Google requires the
      homepage to be on a domain **you own and verify**. Google does not
      accept `*.github.io` as an authorized domain, because you don't own it.
- [ ] **Publish `docs/` with GitHub Pages:** in the repo, go to
      **Settings → Pages → Build and deployment → Deploy from a branch →
      `main` / `/docs`**. The empty `docs/.nojekyll` file makes GitHub serve
      the pages as they are.
- [ ] **Attach the domain:** under **Settings → Pages → Custom domain**,
      enter your domain, add the DNS records GitHub shows, and tick
      **Enforce HTTPS**. The old `github.io` URLs (including the links inside
      the app) then redirect to the domain automatically.
- [ ] **Verify the domain** in
      [Google Search Console](https://search.google.com/search-console)
      with a **Domain** property (DNS TXT record), signed in as a Google
      account that is an **Owner or Editor of the Cloud project**.
- [ ] **Check the site in a private window:** the homepage opens without a
      login, and it links to the Privacy Policy and the Terms.
- [ ] **Create a separate Google Cloud project for production.** Keep the
      current one for development in Testing mode.
- [ ] In the production project, enable **YouTube Data API v3** and create
      an OAuth client of type **Desktop app**.
- [ ] **Release an app version that includes the Accounts-page notice.** The
      Accounts page links to the YouTube Terms of Service, the Bottle Net
      Privacy Policy and Terms, the Google Privacy Policy, and Google's
      permissions page. The YouTube API audit checks for these links.

## 2. Branding page (Google Auth Platform → Branding)

| Field | Value |
| --- | --- |
| App name | `Bottle Net` (must match the name on the homepage exactly) |
| User support email | your support address |
| App logo | 120×120 px PNG/JPG of your own logo. Leave it empty if you have none; adding a logo triggers extra review. |
| Application home page | `https://YOUR-DOMAIN/` |
| Application privacy policy link | `https://YOUR-DOMAIN/privacy.html` |
| Application terms of service link | `https://YOUR-DOMAIN/terms.html` |
| Authorized domains | `YOUR-DOMAIN` |
| Developer contact information | your email |

## 3. Data access (scopes) and justifications

Add exactly these scopes. They match `SCOPES` in
[`bottle_net/publisher/youtube/auth.py`](../bottle_net/publisher/youtube/auth.py).
`youtube.upload` and `youtube.readonly` are *sensitive*; the others are not.

| Scope | Sensitivity |
| --- | --- |
| `https://www.googleapis.com/auth/youtube.upload` | Sensitive |
| `https://www.googleapis.com/auth/youtube.readonly` | Sensitive |
| `openid` | Non-sensitive |
| `https://www.googleapis.com/auth/userinfo.email` | Non-sensitive |

**Justification for `youtube.upload`** (paste into the form):

> Bottle Net is a desktop app that uploads and schedules videos to the user's
> own YouTube channel. The user picks a video file on their computer and
> enters its title, description, tags, privacy status, "made for kids"
> setting, publish time and optional thumbnail. Bottle Net then calls
> videos.insert (resumable upload) to upload the video, and thumbnails.set to
> set the chosen thumbnail. Uploading is the app's core feature and is only
> performed when the user schedules or starts an upload. youtube.upload is the
> narrowest scope that allows videos.insert and thumbnails.set.

**Justification for `youtube.readonly`**:

> Right after sign-in, Bottle Net calls channels.list (part=snippet, mine=true)
> once to read the channel ID and title. The app shows the channel title on
> its Accounts page so the user knows which channel their videos will be
> published to, and it refuses to connect a Google account that has no
> YouTube channel, since uploads to it would fail. No other YouTube data
> (videos, playlists, comments, analytics) is read. youtube.upload does not
> allow channels.list, and youtube.readonly is the narrowest scope that does.

**Why offline access (refresh token) is needed** (if asked):

> Users schedule uploads for a later time. At that time Bottle Net must upload
> without the user present, so it requests access_type=offline and keeps the
> refresh token in the operating system's credential store.

**How the data is used / stored** (if the form asks):

> Bottle Net runs entirely on the user's computer and has no backend server.
> OAuth tokens are stored in the operating system's credential store (Windows
> Credential Manager, macOS Keychain, Linux Secret Service). The channel ID,
> channel title and email are stored in a local database on the user's
> device. No Google user data is sent to the developer or any third party,
> and it is not used for advertising or AI/ML training. The user can revoke
> access from the app's Accounts page (which calls Google's revoke endpoint
> and deletes the tokens and account details) or from Google account
> settings.

## 4. Demo video script

Upload it to YouTube as **Unlisted** and paste the link into the form. Record
the screen in **English** at 1080p. It should run about 3–5 minutes, with no
music, and you can narrate or use on-screen captions. Reviewers check each
**bold** step below. Skipping one is the most common reason for rejection.

1. **Intro (10 s):** "This is Bottle Net, a desktop app that uploads and
   schedules videos to the user's own YouTube channel."
2. **Start the app:** run `bottle-net gui` and show the dashboard opening.
3. **Open Accounts.** Point out the notice with the YouTube Terms of Service
   and privacy policy links, then click **Connect YouTube**.
4. **Consent screen:**
   - Show Google's account chooser and pick a test account.
   - **Zoom into the browser address bar** so the full URL, including
     `client_id=...`, is readable. It must be the production project's
     client ID.
   - **Show the consent screen with the app name "Bottle Net"** and scroll
     so every requested permission is visible.
   - Click **Continue / Allow**.
5. **Back in Bottle Net, show the Accounts page** with the connected email
   and channel title. Say: "This is the only YouTube data we read: the
   channel title and ID from youtube.readonly, so the user sees which
   channel they're publishing to."
6. **Show `youtube.upload` in use:**
   - Add a video **you own** (use your own recording, not a downloaded one).
   - Enter a title, description and tags, choose a thumbnail, set privacy to
     *Private* or *Unlisted*, and click **Publish now**.
   - Show the upload progress and the finished job with its YouTube link.
   - **Open the link in YouTube Studio** and show the uploaded video with the
     title, description and thumbnail you entered.
7. **Optional:** schedule a second upload for a later time and show it in the
   calendar. This demonstrates the scheduling feature that needs a refresh
   token (`access_type=offline`).
8. **Show revocation:** click **Disconnect** on the Accounts page, then show
   that Bottle Net is gone from <https://myaccount.google.com/permissions>.
9. **Show the homepage, Privacy Policy and Terms** on your domain for a few
   seconds each.

## 5. After you submit

- Publish the production project (**Audience → Publish app**) and submit it
  in the **Verification Center**. Brand verification usually takes 2–3
  business days, and sensitive-scope review can take several weeks. Reply to
  reviewer emails promptly; they often ask for a changed video or policy text.
- Don't change scopes, the app name or the policy URLs while the review is
  running; that restarts it.
- **Ship the production client ID and secret** with the app (for example in
  a bundled `client_secret.json` read through
  `BOTTLE_NET_YOUTUBE_CLIENT_SECRETS_FILE`). Because the client is a Desktop
  app, Google does not treat that secret as confidential, and the flow uses
  PKCE.

## 6. YouTube API quota extension and audit

The default quota of 10,000 units a day is shared by **all** users. One upload
costs about 1,600 units, which is about 6 uploads a day in total. Apply with
the [YouTube API Services audit and quota extension form](https://support.google.com/youtube/contact/yt_api_form).
The auditors check the items below; each one is already covered:

| Requirement (YouTube API Developer Policies) | Where it is covered |
| --- | --- |
| Terms say users agree to the YouTube ToS | `terms.html` §1, homepage, Accounts page in the app |
| Privacy policy says the app uses YouTube API Services | `privacy.html` §1 |
| Links to the Google Privacy Policy | `privacy.html` §1, `terms.html` §1, Accounts page |
| Explains what data is accessed, stored, used and shared | `privacy.html` §1 |
| Discloses third-party content, ads and cookies | `privacy.html` §4 (none) |
| Explains revocation via Google security settings, with link | `privacy.html` §6, Accounts page |
| Stored data is deleted after revocation | Disconnect deletes the tokens and account details (`privacy.html` §6) |
| Contact information | `privacy.html` §9, `terms.html` §10 |

Useful answers for the form:

- **Is the API client used by the public?** Yes, it's a free desktop app.
- **Does it store API data?** Only on the user's own computer (channel ID,
  channel title, IDs of videos the user uploaded). No server stores it.
- **Monetization:** none; free and open source.
- **Screenshots/recording:** reuse the demo video from section 4.

## 7. Risk: the download-and-reupload feature

Reviewers, and especially the YouTube API audit, may look closely at an app
that downloads other people's TikTok and Facebook videos and publishes them
to YouTube, because that can look like it facilitates copyright infringement.
Be open about the feature and point out its safeguards:

- The app requires users to confirm that they own a downloaded video or have
  permission to publish it before it can be scheduled. The server enforces
  this, not just the UI (`rights_required` in
  [`bottle_net/publisher/jobs.py`](../bottle_net/publisher/jobs.py)).
- The homepage and the Terms (§4 and §5) state that users may only publish
  content they have rights to.

Do not hide the downloader from reviewers. If they later find something the
submission didn't disclose, they can revoke the approval.

## 8. Facebook (Meta App Review)

Meta's review uses the same pages:

- **Privacy Policy URL:** `https://YOUR-DOMAIN/privacy.html`
- **Terms of Service URL:** `https://YOUR-DOMAIN/terms.html`
- **User data deletion:** choose *Data deletion instructions URL* and enter
  `https://YOUR-DOMAIN/privacy.html#delete`
