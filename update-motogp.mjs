// update-motogp.mjs
// Scarica da Pulselive (API ufficiale MotoGP) e salva nel repository:
//   moto-data.json            -> calendario + classifiche dell'evento in corso (o dell'ultimo disputato)
//   moto-archive/<evento>.json -> classifiche di OGNI gara già iniziata (si completa a poco a poco)
// Il sito legge questi file perché il browser non può chiamare direttamente l'API (blocco 403).
import { readFile, writeFile, mkdir } from 'node:fs/promises';

const API = process.env.MOTOGP_API || 'https://api.motogp.pulselive.com/motogp/v1';
const OUT = process.env.OUT_DIR || '.';
const MAX_BACKFILL = Number(process.env.MAX_BACKFILL || 6); // gare vecchie scaricate per ogni esecuzione
const DAY = 86_400_000;
const CLASSES = {
  MotoGP: 'e8c110ad-64aa-4e8e-8a86-f2f152f6a942',
  Moto2: '549640b8-fd9c-4245-acfd-60e4bc38b25c',
  Moto3: '954f7e65-2ef2-4423-b949-4961cc603e45'
};
const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  'Accept': 'application/json, text/plain, */*',
  'Accept-Language': 'it-IT,it;q=0.9,en;q=0.8',
  'Origin': 'https://www.motogp.com',
  'Referer': 'https://www.motogp.com/'
};

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, tries = 3) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 20_000);
      const res = await fetch(API + path, { headers: HEADERS, signal: ctrl.signal });
      clearTimeout(timer);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (e) {
      last = new Error(`${path}: ${e.message}`);
      await sleep(1500 * (i + 1));
    }
  }
  throw last;
}

// Esegue fn su tutti gli elementi, al massimo `limit` alla volta, restituendo i risultati nello stesso ordine.
async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i], i);
    }
  }));
  return results;
}

const trimEvent = e => ({
  id: e.id, short_name: e.short_name ?? null, name: e.name ?? null,
  date_start: e.date_start ?? null, date_end: e.date_end ?? null, status: e.status ?? null
});
const trimSession = s => ({
  id: s.id, type: s.type ?? null, number: s.number ?? null, status: s.status ?? null, date: s.date ?? null
});
const trimRow = r => ({
  position: r.position ?? null,
  rider: { full_name: r.rider?.full_name ?? null, rider_number: r.rider?.number ?? r.rider?.rider_number ?? null },
  gap: { first: r.gap?.first ?? null },
  total_laps: r.total_laps ?? null
});

async function fetchEventData(eventId) {
  const byClass = {};
  for (const catId of Object.values(CLASSES)) {
    const raw = await api(`/results/sessions?eventUuid=${encodeURIComponent(eventId)}&categoryUuid=${encodeURIComponent(catId)}`);
    const sessions = (Array.isArray(raw) ? raw : []).map(trimSession);
    const finished = sessions.filter(s => s.status === 'FINISHED');
    const rowsList = await mapLimit(finished, 3, async s => {
      try {
        const data = await api(`/results/session/${encodeURIComponent(s.id)}/classification?test=false`);
        return (Array.isArray(data?.classification) ? data.classification : []).map(trimRow);
      } catch (e) {
        console.warn(`  classifica non scaricata (${s.type}): ${e.message}`);
        return [];
      }
    });
    const rowsBy = {};
    finished.forEach((s, i) => { if (rowsList[i].length) rowsBy[s.id] = rowsList[i]; });
    byClass[catId] = { sessions, rowsBy };
  }
  return byClass;
}

async function readJson(path) {
  try { return JSON.parse(await readFile(path, 'utf8')); } catch { return null; }
}

// Scrive il file solo se i dati sono cambiati: "updated" non cambia da solo, così Git non crea commit inutili.
async function writeIfChanged(path, obj) {
  const old = await readJson(path);
  const strip = o => JSON.stringify({ ...o, updated: undefined });
  if (old && strip(old) === strip(obj)) return false;
  await writeFile(path, JSON.stringify({ ...obj, updated: new Date().toISOString() }));
  return true;
}

async function main() {
  const seasons = await api('/results/seasons');
  const year = new Date().getFullYear();
  const season = (Array.isArray(seasons) ? seasons : []).find(s => s.current) || seasons.find(s => Number(s.year) === year);
  if (!season?.id) throw new Error('stagione corrente non trovata');

  const rawEvents = await api(`/results/events?seasonUuid=${encodeURIComponent(season.id)}`);
  const events = (Array.isArray(rawEvents) ? rawEvents : [])
    .filter(e => e.test !== true)
    .map(trimEvent)
    .sort((a, b) => Date.parse(a.date_start) - Date.parse(b.date_start));
  if (!events.length) throw new Error('nessun evento ricevuto');

  const now = Date.now();
  const started = events.filter(e => Date.parse(e.date_start) <= now).sort((a, b) => Date.parse(b.date_start) - Date.parse(a.date_start));
  const endOf = e => (Date.parse(e.date_end) || Date.parse(e.date_start)) + DAY;
  const recent = started.filter(e => endOf(e) + 2 * DAY >= now);   // weekend in corso o appena finito: si riscarica sempre

  await mkdir(`${OUT}/moto-archive`, { recursive: true });
  const archived = new Set();
  for (const e of started) if (await readJson(`${OUT}/moto-archive/${e.id}.json`)) archived.add(e.id);
  const missing = started.filter(e => !archived.has(e.id) && !recent.includes(e)).slice(0, MAX_BACKFILL);
  const todo = [...recent, ...missing];
  console.log(`Eventi: ${events.length}, iniziati: ${started.length}, già in archivio: ${archived.size}, da scaricare ora: ${todo.length}`);

  const fresh = new Map();
  for (const e of todo) {
    try {
      console.log(`Scarico ${e.short_name} (${e.date_start})…`);
      const byClass = await fetchEventData(e.id);
      fresh.set(e.id, byClass);
      const changed = await writeIfChanged(`${OUT}/moto-archive/${e.id}.json`, { eventId: e.id, byClass });
      console.log(`  ${changed ? 'aggiornato' : 'invariato'}`);
    } catch (e2) {
      console.warn(`  ERRORE su ${e.short_name}: ${e2.message}`);
    }
  }

  // moto-data.json: stesso formato di sempre, ma con l'evento più recente (anche se è in corso).
  const prev = await readJson(`${OUT}/moto-data.json`);
  const latest = started[0] || null;
  let eventId = prev?.eventId || null;
  let byClass = prev?.byClass || {};
  if (latest) {
    const data = fresh.get(latest.id) || (await readJson(`${OUT}/moto-archive/${latest.id}.json`))?.byClass;
    if (data) { eventId = latest.id; byClass = data; }
  }
  const changed = await writeIfChanged(`${OUT}/moto-data.json`, { eventId, events, byClass });
  console.log(`moto-data.json ${changed ? 'aggiornato' : 'invariato'} (evento ${latest?.short_name || '-'})`);
}

main().catch(e => { console.error('ERRORE:', e.message); process.exit(1); });
