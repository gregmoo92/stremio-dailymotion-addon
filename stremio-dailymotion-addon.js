// stremio-dailymotion-addon.js
// Simple Stremio add-on that exposes Dailymotion public videos using Dailymotion's Platform API

const { addonBuilder } = require('stremio-addon-sdk');
const fetch = require('node-fetch');
const http = require('http');

const manifest = {
  id: 'org.gabriel.dailymotion',
  version: '1.0.0',
  name: 'Dailymotion (Unofficial)',
  description: 'Search Dailymotion and expose public videos as a Stremio catalog + streams (unofficial).',
  resources: ['catalog', 'stream', 'meta'],
  types: ['movie', 'series', 'episode'],
  idPrefixes: ['dm:'],
  catalogs: [
    { type: 'series', id: 'dailymotion_series', name: 'Dailymotion — Series/Shows (search)' },
    { type: 'movie', id: 'dailymotion_movies', name: 'Dailymotion — Movies (search)' }
  ],
  contactEmail: 'you@example.com',
  links: { homepage: 'https://github.com' }
};

const builder = new addonBuilder(manifest);

async function searchDailymotion(q, limit = 25, page = 1) {
  const fields = ['id', 'title', 'duration', 'thumbnail_url', 'url', 'description'].join(',');
  const params = new URLSearchParams({
    search: q,
    fields,
    limit: String(limit),
    page: String(page)
  });
  const res = await fetch(`https://api.dailymotion.com/videos?${params.toString()}`);
  if (!res.ok) throw new Error(`Dailymotion API error: ${res.status}`);
  const body = await res.json();
  return body;
}

builder.defineCatalogHandler(async ({ type, id, extra }) => {
  try {
    const searchQuery = (extra && extra.search && extra.search.query) || (extra && extra.query) || '';
    if (!searchQuery) return { metas: [] };
    const dm = await searchDailymotion(searchQuery, 25);
    const metas = (dm.list || []).map(v => ({
      id: `dm:${v.id}`,
      type: type || 'movie',
      name: v.title,
      poster: v.thumbnail_url || undefined,
      description: v.description || undefined,
      runtime: v.duration || undefined,
      imdb_id: null
    }));
    return { metas };
  } catch (err) {
    console.error('Catalog error', err);
    return { metas: [] };
  }
});

builder.defineMetaHandler(async ({ type, id }) => {
  try {
    const vid = id.replace(/^dm:/, '');
    const res = await fetch(`https://api.dailymotion.com/video/${vid}?fields=id,title,duration,thumbnail_url,description,url`);
    if (!res.ok) return { meta: {} };
    const v = await res.json();
    return {
      meta: {
        id: `dm:${v.id}`,
        type: type || 'movie',
        name: v.title,
        poster: v.thumbnail_url,
        description: v.description,
        runtime: v.duration
      }
    };
  } catch (err) {
    console.error('Meta error', err);
    return { meta: {} };
  }
});

builder.defineStreamHandler(async ({ id }) => {
  try {
    const vid = id.replace(/^dm:/, '');
    const embedUrl = `https://www.dailymotion.com/embed/video/${vid}`;
    return { streams: [{ title: 'Dailymotion (embed)', url: embedUrl, isFree: true }] };
  } catch (err) {
    console.error('Stream error', err);
    return { streams: [] };
  }
});

const addonInterface = builder.getInterface();
const port = process.env.PORT || 7000;

const server = http.createServer((req, res) => addonInterface(req, res));
server.listen(port, () => console.log(`Stremio Dailymotion addon running on http://localhost:${port}/manifest.json`));
