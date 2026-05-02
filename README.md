# Stremio Dailymotion Addon

Unofficial Stremio addon that surfaces public Dailymotion videos as a configurable catalog.

## Features
- **Trending** catalog (no search needed) and **Search** catalogs for movies/series
- **Genre** filter exposed in the Stremio UI (animals, music, news, sport, tv, videogames, …)
- **Configurable defaults** via a small web UI: sort order, language, country, family-safe filter, page size
- Per-user config is encoded into the install URL — no server-side state
- No Dailymotion API key required

## Install (hosted)
1. Open `https://<your-host>/configure`
2. Pick your defaults
3. Click **Install in Stremio** (or copy the manifest URL into Stremio → Addons → Install via URL)

## Self-host
Any Node 18+ host works (Render, Fly.io, Railway, a VPS, etc.):

```bash
npm install
npm start
```

Then visit `http://localhost:7000/configure`.

### Render
- New Web Service → Node
- Build: `npm install`
- Start: `npm start`
- The configure page lives at `https://<your-service>.onrender.com/configure`

## How configuration works
The configure page base64url-encodes your settings into the URL path:
`/<config>/manifest.json`. Each request carries the config, so swapping defaults
just means installing a new manifest URL — nothing is persisted server-side.

## Notes
- Streams are Dailymotion embed URLs; playback may be limited on some Stremio
  platforms (notably web in certain browsers).
- For personal use only. Respect Dailymotion's Terms of Service.
