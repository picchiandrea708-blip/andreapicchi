const season = 'e88b4e43-2209-47aa-8e83-0e0b1cedde6e';
const classes = [
  'e8c110ad-64aa-4e8e-8a86-f2f152f6a942',
  '549640b8-fd9c-4245-acfd-60e4bc38b25c',
  '954f7e65-2ef2-4423-b949-4961cc603e45'
];
const base = 'https://api.motogp.pulselive.com/motogp/v1';
async function get(path) {
  const res = await fetch(base + path, { headers: { 'User-Agent': 'Mozilla/5.0', Accept: 'application/json' } });
  if (!res.ok) throw new Error(path + ' ' + res.status);
  return res.json();
}
const events = await get('/results/events?seasonUuid=' + season);
const list = events.filter(e => e && !e.test).map(e => ({
  id: e.id, short_name: e.short_name, name: e.name, date_start: e.date_start, date_end: e.date_end, status: e.status
}));
const now = Date.now();
const current = list.find(e => Date.parse(e.date_start) <= now && Date.parse(e.date_end || e.date_start) + 86400000 >= now)
  || [...list].reverse().find(e => Date.parse(e.date_start) <= now)
  || list.at(-1);
const byClass = {};
for (const cid of classes) {
  const sessions = await get('/results/sessions?eventUuid=' + current.id + '&categoryUuid=' + cid);
  const slim = sessions.map(s => ({ id: s.id, type: s.type, number: s.number, status: s.status, date: s.date }));
  const rowsBy = {};
  for (const s of slim) {
    try {
      const cl = await get('/results/session/' + s.id + '/classification?test=false');
      rowsBy[s.id] = (cl.classification || []).map(r => ({
        position: r.position,
        rider: { full_name: r.rider && r.rider.full_name },
        gap: { first: r.gap && r.gap.first },
        total_laps: r.total_laps
      }));
    } catch (e) {
      rowsBy[s.id] = [];
    }
  }
  byClass[cid] = { sessions: slim, rowsBy };
}
const out = { eventId: current.id, updated: new Date().toISOString(), events: list, byClass };
await import('node:fs').then(fs => fs.writeFileSync('moto-data.json', JSON.stringify(out)));
console.log('saved', current.short_name, current.status);
