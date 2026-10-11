// probe-f1.mjs — PROVA di connessione al live timing ufficiale F1 dai server di GitHub.
// Prova entrambi i protocolli (classico e "core"), senza abbonamento, e scrive cosa arriva davvero.
// Il risultato viene pubblicato sul ramo "f1probe" (file f1-probe.json) e letto dalla Diagnostica del sito.
import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const BASE = process.env.F1_BASE || 'https://livetiming.formula1.com';
const WS_BASE = BASE.replace(/^http/, 'ws');
const LISTEN_MS = Number(process.env.LISTEN_S || 25) * 1000;
const BRANCH = process.env.PUBLISH_BRANCH || 'f1probe';
const REMOTE = process.env.PUBLISH_REMOTE ||
  `https://x-access-token:${process.env.GITHUB_TOKEN}@github.com/${process.env.GITHUB_REPOSITORY}.git`;
const TOPICS = ['Heartbeat', 'SessionInfo', 'SessionStatus', 'DriverList', 'TimingData', 'LapCount', 'TrackStatus', 'ExtrapolatedClock', 'WeatherData', 'RaceControlMessages'];
const UA = { 'User-Agent': 'BestHTTP', 'Accept-Encoding': 'gzip,identity' };
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// Riassunto leggibile di un argomento (topic) ricevuto
function describe(topic, v) {
  try {
    if (v == null) return 'vuoto';
    if (typeof v === 'string') return `testo (${v.length} car.)`;
    if (topic === 'TimingData') return `${Object.keys(v.Lines || {}).length} piloti`;
    if (topic === 'DriverList') return `${Object.keys(v).length} piloti`;
    if (topic === 'SessionInfo') return `${v.Meeting?.Name || '?'} / ${v.Name || '?'}`;
    if (topic === 'SessionStatus') return `stato ${v.Status || '?'}`;
    if (topic === 'TrackStatus') return `stato ${v.Status || '?'} ${v.Message || ''}`.trim();
    if (topic === 'LapCount') return `giro ${v.CurrentLap ?? '?'}/${v.TotalLaps ?? '?'}`;
    return `${Object.keys(v).length} campi`;
  } catch { return 'illeggibile'; }
}

// Apre un WebSocket con intestazioni personalizzate e raccoglie i messaggi per un po'.
function wsSession(url, headers, { onOpen, onMessage }) {
  return new Promise(resolve => {
    const out = { opened: false, messages: 0, closed: null, error: null };
    let ws, timer;
    const finish = () => { clearTimeout(timer); try { ws && ws.close(); } catch {} resolve(out); };
    try { ws = new WebSocket(url, { headers }); } catch (e) { out.error = 'apertura: ' + e.message; return resolve(out); }
    ws.binaryType = 'arraybuffer';
    timer = setTimeout(finish, LISTEN_MS);
    ws.addEventListener('open', () => { out.opened = true; try { onOpen(ws); } catch (e) { out.error = 'invio: ' + e.message; } });
    ws.addEventListener('message', ev => {
      const text = typeof ev.data === 'string' ? ev.data : Buffer.from(ev.data).toString('utf8');
      out.messages++;
      try { onMessage(ws, text); } catch (e) { out.error = out.error || 'lettura: ' + e.message; }
    });
    ws.addEventListener('error', ev => { out.error = out.error || ('connessione: ' + (ev.message || 'errore')); });
    ws.addEventListener('close', ev => { out.closed = `${ev.code}${ev.reason ? ' ' + ev.reason : ''}`; finish(); });
  });
}

function newCollector() {
  return { initial: {}, feedCount: 0, feedTopics: {}, errors: [] };
}
function summarize(name, res, col, extraErr) {
  const topics = Object.entries(col.initial).map(([t, d]) => `${t}: ${d}`).join('; ');
  const feeds = Object.entries(col.feedTopics).map(([t, n]) => `${t}×${n}`).join(', ');
  if (!res.opened) return `${name}: NON connesso${res.error ? ' (' + res.error + ')' : ''}${res.closed ? ' chiuso ' + res.closed : ''}${extraErr ? ' - ' + extraErr : ''}`;
  return `${name}: connesso, ${res.messages} messaggi. Stato iniziale: ${topics || 'nessuno'}. Aggiornamenti: ${feeds || 'nessuno'}.${col.errors.length ? ' Errori: ' + col.errors.slice(0, 2).join(' | ') : ''}${res.closed ? ' Chiuso: ' + res.closed : ''}`;
}

// --- Protocollo classico (ASP.NET SignalR 1.5) ---
async function probeLegacy() {
  const r = { protocol: 'classico' };
  try {
    const hub = encodeURIComponent(JSON.stringify([{ name: 'Streaming' }]));
    const neg = await fetch(`${BASE}/signalr/negotiate?connectionData=${hub}&clientProtocol=1.5`, { headers: UA });
    r.negotiate = neg.status;
    if (!neg.ok) { r.summary = `classico: negotiate HTTP ${neg.status}`; return r; }
    const body = await neg.json();
    if (!body.ConnectionToken) { r.summary = 'classico: negotiate senza token'; return r; }
    const cookie = (neg.headers.getSetCookie ? neg.headers.getSetCookie() : []).map(c => c.split(';')[0]).join('; ');
    const url = `${WS_BASE}/signalr/connect?clientProtocol=1.5&transport=webSockets&connectionToken=${encodeURIComponent(body.ConnectionToken)}&connectionData=${hub}`;
    const col = newCollector();
    const res = await wsSession(url, { ...UA, Cookie: cookie }, {
      onOpen: ws => ws.send(JSON.stringify({ H: 'Streaming', M: 'Subscribe', A: [TOPICS], I: 1 })),
      onMessage: (ws, text) => {
        const m = JSON.parse(text);
        if (m.R && typeof m.R === 'object') for (const [t, v] of Object.entries(m.R)) col.initial[t] = describe(t, v);
        if (m.E) col.errors.push(String(m.E).slice(0, 80));
        for (const f of m.M || []) if (f.M === 'feed') { col.feedCount++; const t = f.A?.[0]; col.feedTopics[t] = (col.feedTopics[t] || 0) + 1; }
      }
    });
    r.opened = res.opened; r.messages = res.messages; r.initial = col.initial; r.feed = col.feedTopics;
    r.summary = summarize('classico', res, col);
  } catch (e) { r.summary = 'classico: errore ' + e.message; }
  return r;
}

// --- Protocollo "core" (ASP.NET Core SignalR) ---
async function probeCore() {
  const r = { protocol: 'core' };
  try {
    const neg = await fetch(`${BASE}/signalrcore/negotiate?negotiateVersion=1`, { method: 'POST', headers: UA });
    r.negotiate = neg.status;
    if (!neg.ok) { r.summary = `core: negotiate HTTP ${neg.status}${neg.status === 401 || neg.status === 403 ? ' (probabile accesso riservato: serve abbonamento F1 TV)' : ''}`; return r; }
    const body = await neg.json();
    const id = body.connectionToken || body.connectionId;
    if (!id) { r.summary = 'core: negotiate senza identificativo'; return r; }
    const cookie = (neg.headers.getSetCookie ? neg.headers.getSetCookie() : []).map(c => c.split(';')[0]).join('; ');
    const url = `${WS_BASE}/signalrcore?id=${encodeURIComponent(id)}`;
    const col = newCollector();
    let handshaken = false;
    const res = await wsSession(url, { ...UA, ...(cookie ? { Cookie: cookie } : {}) }, {
      onOpen: ws => ws.send(JSON.stringify({ protocol: 'json', version: 1 }) + '\x1e'),
      onMessage: (ws, text) => {
        for (const part of text.split('\x1e').filter(Boolean)) {
          const m = JSON.parse(part);
          if (!handshaken && (m.error || Object.keys(m).length === 0)) {
            if (m.error) col.errors.push('handshake: ' + String(m.error).slice(0, 80));
            handshaken = true;
            ws.send(JSON.stringify({ type: 1, invocationId: '1', target: 'Subscribe', arguments: [TOPICS] }) + '\x1e');
            continue;
          }
          if (m.type === 3) {
            if (m.error) col.errors.push(String(m.error).slice(0, 80));
            if (m.result && typeof m.result === 'object') for (const [t, v] of Object.entries(m.result)) col.initial[t] = describe(t, v);
          } else if (m.type === 1 && m.target === 'feed') {
            col.feedCount++; const t = m.arguments?.[0]; col.feedTopics[t] = (col.feedTopics[t] || 0) + 1;
          } else if (m.type === 7) col.errors.push('chiusura dal server: ' + String(m.error || '').slice(0, 80));
        }
      }
    });
    r.opened = res.opened; r.messages = res.messages; r.initial = col.initial; r.feed = col.feedTopics;
    r.summary = summarize('core', res, col);
  } catch (e) { r.summary = 'core: errore ' + e.message; }
  return r;
}

function publish(obj) {
  const dir = mkdtempSync(join(tmpdir(), 'f1probe-'));
  const git = (...a) => execFileSync('git', ['-C', dir, '-c', 'user.name=f1-probe-bot', '-c', 'user.email=f1-probe-bot@users.noreply.github.com', ...a], { stdio: 'pipe' });
  try {
    git('init', '-q'); git('checkout', '-q', '-b', BRANCH); git('remote', 'add', 'origin', REMOTE);
    writeFileSync(join(dir, 'f1-probe.json'), JSON.stringify(obj));
    git('add', 'f1-probe.json'); git('commit', '-q', '-m', 'prova F1');
    git('push', '-q', '--force', 'origin', `HEAD:refs/heads/${BRANCH}`);
    return true;
  } catch (e) {
    log('pubblicazione fallita:', String(e.stderr || e.message).replace(/x-access-token:[^@]+@/g, 'x-access-token:***@').slice(0, 200));
    return false;
  }
}

const legacy = await probeLegacy(); log(legacy.summary);
const core = await probeCore(); log(core.summary);
const ok = publish({ generatedAt: new Date().toISOString(), legacy, core });
log(ok ? `risultato pubblicato sul ramo ${BRANCH}` : 'risultato NON pubblicato');
process.exit(ok ? 0 : 1);
