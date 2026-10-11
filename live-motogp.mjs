// live-motogp.mjs
// Durante le sessioni MotoGP scarica il timing in diretta (dai server di GitHub, che MotoGP non blocca)
// e lo pubblica sul ramo "live" del repository come moto-live.json. Il sito legge quel file.
// Se non c'è nessuna sessione in corso esce subito senza scrivere nulla.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const API = process.env.MOTOGP_API || 'https://api.motogp.pulselive.com/motogp/v1';
const POLL_MS = Number(process.env.POLL_S || 10) * 1000;        // ogni quanto interroga MotoGP
const PUBLISH_MS = Number(process.env.PUBLISH_S || 20) * 1000;  // ogni quanto pubblica il file
const MAX_MS = Number(process.env.MAX_MINUTES || 55) * 60_000;  // durata massima di una esecuzione
const IDLE_POLLS = Number(process.env.IDLE_POLLS || 6);         // controlli "non live" di fila prima di fermarsi
const BRANCH = process.env.PUBLISH_BRANCH || 'live';
const REMOTE = process.env.PUBLISH_REMOTE ||
  `https://x-access-token:${process.env.GITHUB_TOKEN}@github.com/${process.env.GITHUB_REPOSITORY}.git`;
const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36',
  'Accept': 'application/json, text/plain, */*',
  'Accept-Language': 'it-IT,it;q=0.9,en;q=0.8',
  'Origin': 'https://www.motogp.com',
  'Referer': 'https://www.motogp.com/'
};
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// Si lavora solo nel weekend di gara (dal calendario salvato in moto-data.json).
function weekendActive() {
  try {
    const events = JSON.parse(readFileSync('moto-data.json', 'utf8')).events || [];
    const now = Date.now();
    return events.some(e => {
      const s = Date.parse(e.date_start), en = Date.parse(e.date_end);
      return Number.isFinite(s) && now >= s - 12 * 3_600_000 && now <= (Number.isFinite(en) ? en : s) + 36 * 3_600_000;
    });
  } catch { return true; } // calendario non leggibile: meglio controllare comunque
}

async function getLive() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15_000);
  try {
    const res = await fetch(`${API}/timing-gateway/livetiming-lite`, { headers: HEADERS, signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    if (!text.trim() || text.trim().startsWith('<')) throw new Error('risposta non JSON');
    return JSON.parse(text);
  } finally { clearTimeout(timer); }
}

// Stessa regola usata dal sito: sessione avviata ("S") oppure bandiera rossa ("R") con tempi validi.
function isLive(data) {
  const riders = Object.values(data?.rider || {});
  if (!riders.length) return false;
  const status = String(data?.head?.session_status_id || '').toUpperCase();
  const hasTiming = riders.some(r => Number(r.pos) > 0 && (Number(r.total_laps) > 0 ||
    [r.lap_time, r.last_lap_time].some(t => t && !/^0+(\.0+)?$/.test(String(t)))));
  return status === 'S' || (status === 'R' && hasTiming);
}

function makePublisher() {
  const dir = mkdtempSync(join(tmpdir(), 'motolive-'));
  const git = (...args) => execFileSync('git', ['-C', dir, '-c', 'user.name=motogp-live-bot',
    '-c', 'user.email=motogp-live-bot@users.noreply.github.com', ...args], { stdio: 'pipe' });
  git('init', '-q'); git('checkout', '-q', '-b', BRANCH); git('remote', 'add', 'origin', REMOTE);
  let committed = false, failures = 0;
  return {
    publish(obj) {
      try {
        writeFileSync(join(dir, 'moto-live.json'), JSON.stringify(obj));
        git('add', 'moto-live.json');
        if (!committed) { git('commit', '-q', '-m', 'live'); committed = true; }
        else git('commit', '-q', '--amend', '-m', 'live');   // un solo commit: la cronologia non cresce
        git('push', '-q', '--force', 'origin', `HEAD:refs/heads/${BRANCH}`);
        failures = 0;
        return true;
      } catch (e) {
        failures++;
        const msg = String(e.stderr || e.message).replace(/x-access-token:[^@]+@/g, 'x-access-token:***@').slice(0, 200);
        log(`pubblicazione fallita (${failures}): ${msg}`);
        if (failures >= 5) throw new Error('pubblicazione fallita 5 volte di fila');
        return false;
      }
    }
  };
}

async function main() {
  // Modalità di prova (Run workflow con "test" attivo): controlla l'API MotoGP e pubblica un file di prova,
  // anche senza sessioni in corso. Serve a verificare permessi di scrittura e accesso a MotoGP.
  if (process.env.TEST_PUBLISH) {
    const info = {};
    try {
      const d = await getLive();
      info.api = 'ok';
      info.stato = d.head?.session_status_id || '?';
      info.piloti = Object.keys(d.rider || {}).length;
      info.sessione = [d.head?.event_shortname, d.head?.category, d.head?.session_name].filter(Boolean).join(' ');
    } catch (e) { info.api = 'errore: ' + e.message; }
    log('controllo API MotoGP:', JSON.stringify(info));
    const ok = makePublisher().publish({ generatedAt: new Date().toISOString(), live: false, test: true, apiCheck: info });
    log(ok ? 'file di prova pubblicato sul ramo live' : 'pubblicazione NON riuscita');
    if (!ok) throw new Error('pubblicazione non riuscita (permessi di scrittura?)');
    return;
  }
  if (!process.env.FORCE && !weekendActive()) { log('nessun weekend di gara: esco'); return; }
  const deadline = Date.now() + MAX_MS;
  const pub = makePublisher();
  let wasLive = false, idle = 0, fails = 0, lastPublish = 0, published = 0;

  while (Date.now() < deadline) {
    let data;
    try { data = await getLive(); fails = 0; }
    catch (e) {
      fails++; log(`lettura fallita (${fails}): ${e.message}`);
      if (fails >= 6) throw new Error('MotoGP non raggiungibile: ' + e.message);
      await sleep(POLL_MS); continue;
    }
    if (isLive(data)) {
      idle = 0;
      if (!wasLive) log(`sessione LIVE: ${data.head?.event_shortname || ''} ${data.head?.category || ''} ${data.head?.session_name || ''}`);
      wasLive = true;
      if (Date.now() - lastPublish >= PUBLISH_MS) {
        if (pub.publish({ generatedAt: new Date().toISOString(), live: true, data })) { lastPublish = Date.now(); published++; }
      }
    } else {
      if (!wasLive) { log(`nessuna sessione in corso (stato ${data.head?.session_status_id || '?'}, piloti ${Object.keys(data.rider || {}).length}): esco`); return; }
      idle++;
      if (idle >= IDLE_POLLS) {
        log('sessione terminata');
        pub.publish({ generatedAt: new Date().toISOString(), live: false });
        return;
      }
    }
    await sleep(POLL_MS);
  }
  log(`tempo massimo raggiunto (pubblicazioni: ${published})`);
}

main().catch(e => { console.error('ERRORE:', e.message); process.exit(1); });
