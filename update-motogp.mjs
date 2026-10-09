const season = process.env.MOTOGP_SEASON_UUID || 'e88b4e43-2209-47aa-8e83-0e0b1cedde6e';
const classes = [
  'e8c110ad-64aa-4e8e-8a86-f2f152f6a942',
  '549640b8-fd9c-4245-acfd-60e4bc38b25c',
  '954f7e65-2ef2-4423-b949-4961cc603e45'
];
const base = 'https://api.motogp.pulselive.com/motogp/v1';
const now = Date.now();

async function get(path) {
  const res = await fetch(base + path, {
    headers: { 'User-Agent': 'Mozilla/5.0', Accept: 'application/json' },
    signal: AbortSignal.timeout(15_000)
  });
  if (!res.ok) throw new Error(path + ' ' + res.status);
  return res.json();
}

function endOfEvent(value) {
  if (!value) return NaN;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return Date.parse(`${value}T23:59:59Z`);
  return Date.parse(value);
}
function eventIsCompleted(event) {
  const status = String(event.status || '').toUpperCase();
  if (/CURRENT|NOT.?STARTED|SCHEDULED/.test(status)) return false;
  return /FINISHED|FINAL|ENDED|COMPLETED/.test(status) || endOfEvent(event.date_end) < now;
}
function sessionIsCompleted(session, eventCompleted) {
  const status = String(session.status || session.state || '').toUpperCase();
  if (/CANCEL|POSTPON/.test(status)) return false;
  if (/^(FINISHED|FINISH|FINAL|COMPLETED|ENDED|F)$/.test(status)) return true;
  if (/^(LIVE|IN.?PROGRESS|RUNNING|STARTED|S|NOT.?STARTED|SCHEDULED|N)$/.test(status)) return false;
  return eventCompleted && Date.parse(session.date || '') <= now;
}

const responseEvents = await get('/results/events?seasonUuid=' + encodeURIComponent(season));
const events = (Array.isArray(responseEvents) ? responseEvents : [])
  .filter(event => event && !event.test)
  .map(event => ({
    id: event.id,
    short_name: event.short_name,
    name: event.name,
    date_start: event.date_start,
    date_end: event.date_end,
    status: event.status
  }));
if (!events.length) throw new Error('MotoGP: nessun evento di stagione');

const completedEvents = events.filter(eventIsCompleted)
  .sort((a, b) => endOfEvent(b.date_end) - endOfEvent(a.date_end));
const archiveEvent = completedEvents[0];
if (!archiveEvent?.id) throw new Error('MotoGP: nessun evento concluso disponibile per l’archivio');

const byClass = {};
for (const categoryUuid of classes) {
  const responseSessions = await get(`/results/sessions?eventUuid=${encodeURIComponent(archiveEvent.id)}&categoryUuid=${encodeURIComponent(categoryUuid)}`);
  const sessions = (Array.isArray(responseSessions) ? responseSessions : [])
    .filter(session => session && !session.test)
    .map(session => ({
      id: session.id,
      type: session.type,
      number: session.number,
      status: session.status,
      date: session.date
    }))
    .sort((a, b) => Date.parse(a.date || '') - Date.parse(b.date || ''));

  const rowsBy = {};
  for (const session of sessions) {
    if (!sessionIsCompleted(session, true)) {
      rowsBy[session.id] = [];
      continue;
    }
    try {
      const result = await get(`/results/session/${encodeURIComponent(session.id)}/classification?test=false`);
      rowsBy[session.id] = (Array.isArray(result.classification) ? result.classification : []).map(row => ({
        position: row.position,
        rider: {
          full_name: row.rider?.full_name,
          rider_number: row.rider?.rider_number || row.rider?.number
        },
        gap: { first: row.gap?.first },
        total_laps: row.total_laps
      }));
    } catch (error) {
      console.warn('MotoGP classification unavailable', session.id, error.message);
      rowsBy[session.id] = [];
    }
  }
  byClass[categoryUuid] = { sessions, rowsBy };
}

const output = {
  eventId: archiveEvent.id,
  updated: new Date().toISOString(),
  events,
  byClass
};
await import('node:fs').then(fs => fs.writeFileSync('moto-data.json', JSON.stringify(output)));
console.log('saved archive', archiveEvent.short_name, archiveEvent.status);
