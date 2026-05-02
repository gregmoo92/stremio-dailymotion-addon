// Stremio Dailymotion Addon
// Configurable HTTP server exposing Dailymotion's public videos API as a Stremio catalog.
// Per-user config is encoded into the URL path: /<base64url-config>/manifest.json

const http = require('http');
const fs = require('fs');
const path = require('path');
const fetch = require('node-fetch');

const VERSION = '1.1.0';
const PORT = process.env.PORT || 7000;

// Dailymotion top-level channels usable as a `channel=` filter on /videos
const GENRES = [
  'animals', 'auto', 'creation', 'fun', 'kids', 'life', 'music',
  'news', 'people', 'school', 'shortfilms', 'sport', 'travel', 'tv', 'videogames'
];

const DEFAULT_CONFIG = {
  sort: 'trending',   // trending | recent | relevance | visited
  safe: true,         // family_filter
  lang: '',           // ISO 639-1, '' = any
  country: '',        // ISO 3166-1, '' = any
  limit: 25           // page size (10/25/50/100)
};

const CONFIGURE_HTML = fs.readFileSync(path.join(__dirname, 'configure.html'), 'utf8');

function buildManifest(config) {
  const extra = [
    { name: 'genre', options: GENRES, isRequired: false },
    { name: 'skip', isRequired: false }
  ];
  return {
    id: 'org.gabriel.dailymotion',
    version: VERSION,
    name: 'Dailymotion (Unofficial)',
    description: `Browse and search Dailymotion. Sort: ${config.sort}, lang: ${config.lang || 'any'}, safe: ${config.safe ? 'on' : 'off'}.`,
    resources: ['catalog', 'stream', 'meta'],
    types: ['movie', 'series'],
    idPrefixes: ['dm:'],
    catalogs: [
      {
        type: 'movie',
        id: 'dailymotion_trending',
        name: 'Dailymotion — Trending',
        extra
      },
      {
        type: 'movie',
        id: 'dailymotion_search_movies',
        name: 'Dailymotion — Search',
        extra: [
          { name: 'search', isRequired: true },
          { name: 'genre', options: GENRES, isRequired: false },
          { name: 'skip', isRequired: false }
        ]
      },
      {
        type: 'series',
        id: 'dailymotion_search_series',
        name: 'Dailymotion — Series search',
        extra: [
          { name: 'search', isRequired: true },
          { name: 'genre', options: GENRES, isRequired: false },
          { name: 'skip', isRequired: false }
        ]
      }
    ],
    behaviorHints: { configurable: true, configurationRequired: false },
    logo: 'https://www.dailymotion.com/favicon.ico'
  };
}

function decodeConfig(seg) {
  try {
    const json = Buffer.from(seg, 'base64url').toString('utf8');
    const cfg = JSON.parse(json);
    return { ...DEFAULT_CONFIG, ...cfg };
  } catch {
    return null;
  }
}

async function dmSearch({ search, sort, safe, lang, country, limit, page, channel }) {
  const fields = ['id', 'title', 'duration', 'thumbnail_url', 'description'].join(',');
  const params = new URLSearchParams({ fields, limit: String(limit), page: String(page) });
  if (search) params.set('search', search);
  if (sort) params.set('sort', sort);
  if (safe) params.set('family_filter', 'true');
  if (lang) params.set('language', lang);
  if (country) params.set('country', country);
  if (channel) params.set('channel', channel);
  const res = await fetch(`https://api.dailymotion.com/videos?${params.toString()}`);
  if (!res.ok) throw new Error(`Dailymotion API ${res.status}`);
  return res.json();
}

function videoToMeta(v, type) {
  return {
    id: `dm:${v.id}`,
    type,
    name: v.title,
    poster: v.thumbnail_url || undefined,
    description: v.description || undefined,
    runtime: v.duration ? `${Math.round(v.duration / 60)} min` : undefined
  };
}

async function handleCatalog(config, type, id, extra) {
  const isSearch = id.includes('search');
  const search = extra.search || '';
  if (isSearch && !search) return { metas: [] };

  const skip = parseInt(extra.skip, 10) || 0;
  const limit = config.limit;
  const page = Math.floor(skip / limit) + 1;

  // Trending catalog ignores the user's "sort" preference; search uses it
  // (Dailymotion rejects sort=trending alongside `search=`, so fall back)
  let sort;
  if (!isSearch) sort = 'trending';
  else sort = config.sort === 'trending' ? 'relevance' : config.sort;

  try {
    const dm = await dmSearch({
      search: isSearch ? search : '',
      sort,
      safe: config.safe,
      lang: config.lang,
      country: config.country,
      limit,
      page,
      channel: extra.genre || ''
    });
    return { metas: (dm.list || []).map(v => videoToMeta(v, type)) };
  } catch (err) {
    console.error('catalog error:', err.message);
    return { metas: [] };
  }
}

async function handleMeta(type, id) {
  const vid = id.replace(/^dm:/, '');
  const res = await fetch(`https://api.dailymotion.com/video/${vid}?fields=id,title,duration,thumbnail_url,description`);
  if (!res.ok) return { meta: {} };
  const v = await res.json();
  return { meta: videoToMeta(v, type) };
}

function handleStream(id) {
  const vid = id.replace(/^dm:/, '');
  return {
    streams: [{
      title: 'Dailymotion (embed)',
      externalUrl: `https://www.dailymotion.com/video/${vid}`,
      url: `https://www.dailymotion.com/embed/video/${vid}`,
      isFree: true,
      behaviorHints: { notWebReady: true }
    }]
  };
}

function parseExtra(s) {
  if (!s) return {};
  const out = {};
  for (const pair of s.split('&')) {
    if (!pair) continue;
    const idx = pair.indexOf('=');
    const k = idx === -1 ? pair : pair.slice(0, idx);
    const v = idx === -1 ? '' : pair.slice(idx + 1);
    out[decodeURIComponent(k)] = decodeURIComponent(v);
  }
  return out;
}

function send(res, code, body, contentType = 'application/json') {
  const headers = {
    'Content-Type': contentType,
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': '*',
    'Cache-Control': 'public, max-age=300'
  };
  res.writeHead(code, headers);
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*'
    });
    return res.end();
  }

  try {
    const url = new URL(req.url, 'http://x');
    let pathname = url.pathname.replace(/^\/+|\/+$/g, '');
    let config = { ...DEFAULT_CONFIG };
    const segs = pathname ? pathname.split('/') : [];

    // Optional first segment: base64url config
    if (segs.length && segs[0] !== 'configure' && segs[0] !== 'manifest.json' && /^[A-Za-z0-9_-]{4,}$/.test(segs[0])) {
      const decoded = decodeConfig(segs[0]);
      if (decoded) {
        config = decoded;
        segs.shift();
      }
    }
    pathname = segs.join('/');

    if (pathname === '' || pathname === 'configure') {
      return send(res, 200, CONFIGURE_HTML, 'text/html; charset=utf-8');
    }

    if (pathname === 'manifest.json') {
      return send(res, 200, buildManifest(config));
    }

    let m = pathname.match(/^catalog\/([^/]+)\/([^/]+?)(?:\/([^/]+))?\.json$/);
    if (m) {
      const [, type, id, extraStr] = m;
      return send(res, 200, await handleCatalog(config, type, id, parseExtra(extraStr)));
    }

    m = pathname.match(/^meta\/([^/]+)\/([^/]+)\.json$/);
    if (m) {
      const [, type, id] = m;
      return send(res, 200, await handleMeta(type, id));
    }

    m = pathname.match(/^stream\/([^/]+)\/([^/]+)\.json$/);
    if (m) {
      const [, , id] = m;
      return send(res, 200, handleStream(id));
    }

    send(res, 404, { error: 'not found' });
  } catch (err) {
    console.error(err);
    send(res, 500, { error: String(err.message || err) });
  }
});

server.listen(PORT, () => {
  console.log(`Stremio Dailymotion addon v${VERSION} listening on :${PORT}`);
  console.log(`Configure: http://localhost:${PORT}/configure`);
});
