# Bus & Car Gallery

A simple website for uploading and searching photos of buses and cars.
Built with **Flask** (Python) + **SQLite** — no complicated framework, just
plain, readable code.

## What it does

- **Homepage**: 6 category tiles (Biler, Busser, Lastbiler, Sporvogne, Tog, Andet) — click one to browse just that category.
- Each category page has a **search field** (only appears once you're inside a category) plus **clickable tag chips** for quick filtering (brand, country, etc.).
- Every photo has a **title**, an optional **caption** (free text shown under the title), and free-typed **tags** — put vehicle type, brand, model, country, and "taken in X" as tags, e.g.:
  `bus, volvo, b10m, sweden, taget i norge`
- Searching matches title + caption + tags + album name. Typing more than one word (e.g. `toyota greenland`) requires **all** words to match.
- **Full-size photo view** that scales to fit the browser, with **Forrige/Næste** buttons to browse through the current category (or album) without going back.
- **EXIF date** ("date taken") is read automatically from the photo file and shown next to it; falls back to the upload date if the photo has no such metadata.
- **Bulk upload (Flickr-style)**: choose or drag in 10-50 photos at once. They appear in a grid; click to select (Ctrl/Cmd-click for several, Shift-click for a range, or "All"), then set category, caption, tags and album for everything selected from the left panel. Titles start as the filename - click a name to rename it. Photos upload one at a time with a progress bar each, so a failed photo can simply be retried. Category is required.
- **Download button** (the original file, saved with the photo's title as file name) with a note asking people not to reuse the photo on social media or elsewhere without permission.
- **Comments**: open to everyone, shown immediately, no login needed. A hidden honeypot field filters out basic bots.
- **Contact**: a simple "Kontakt" link in the nav that opens the visitor's own email app (no mail server setup needed). Set the `CONTACT_EMAIL` environment variable to enable it — the link is hidden if it isn't set.
- **Statistik page** (`/statistik`, only visible when logged in): total site visits, and per-image click counts.
- **Albums** (optional, independent of category): group photos across categories, e.g. "Norgestur 2024". Manage from the "Albummer" page.
- Mobile-friendly throughout.

## Running it locally (to try it out first)

You'll need Python 3.9+ installed.

```bash
cd bus-car-gallery
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt

export GALLERY_DEV=1                       # local testing mode (plain http, no strict checks)
export UPLOAD_PASSWORD=pick-a-password     # on Windows: set GALLERY_DEV=1 / set UPLOAD_PASSWORD=...
python app.py
```

Then open http://127.0.0.1:5000 and use "Log in" with the password above.
Photos and the database go into a `data/` folder next to the code
(safe to delete while testing). Run the automated tests with
`pip install -r requirements-dev.txt && pytest`.

## Running it for real (Cloudflare R2 + a small server)

The photos live in a **Cloudflare R2** bucket and are loaded by visitors'
browsers straight from there (cached by Cloudflare's CDN). The Flask server
only holds the small SQLite database and runs the pages. See
**[OPSAETNING.md](OPSAETNING.md)** (Danish) for the step-by-step guide:
domain, R2 bucket, moving the existing photos, hosting, Cloudflare protection
and backups. All settings are listed in `.env.example`.

In production the app **refuses to start** unless `SECRET_KEY` and
`ADMIN_PASSWORD_HASH` are set (create the hash with
`python tools/make_password_hash.py`).

### Security features

- Admin password stored only as a salted hash; login locked per IP address after 5 wrong tries (15 minutes).
- CSRF protection on every form and upload; login cookie is HttpOnly, Secure and SameSite.
- Redirect after login limited to this site; security headers (CSP, X-Frame-Options, ...).
- Comments are rate limited per IP (5 per 10 minutes); the admin can delete comments.
- Optional `ORIGIN_SECRET` so the server only answers requests that came through Cloudflare.

## Growing the collection over the years

- SQLite (the database used here) comfortably handles tens of thousands of
  entries (tested with 50,000 photos: pages render in about 5-10 ms).
- The main cost driver as the collection grows is R2 storage for the photo
  files (about $0.015 per GB per month; downloads from R2 cost nothing).
