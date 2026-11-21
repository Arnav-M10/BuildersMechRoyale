// Play one or more live matches on play.joinbuilders.org with our bot.
// usage: node dev/live/bridge.mjs <room> <matches> <outdir> -- python3 -u bot.py
// Logs every server message (obs etc.) to <outdir>/m<N>.jsonl and the bot trace to <outdir>/m<N>.trace
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
const args = process.argv.slice(2);
const sep = args.indexOf('--');
const [room, matches, outdir] = args.slice(0, sep);
const cmd = args.slice(sep + 1);
fs.mkdirSync(outdir, { recursive: true });
const NAME = 'oval-warden-test';
const sid = crypto.randomBytes(8).toString('hex');
let played = 0, bot = null, log = null, ws = null, inMatch = false;
function startBot(n) {
  const env = { ...process.env, MF_TRACE: `${outdir}/m${n}.trace` };
  bot = spawn(cmd[0], cmd.slice(1), { env, stdio: ['pipe', 'pipe', 'inherit'] });
  let buf = '';
  bot.stdout.on('data', (d) => {
    buf += d.toString();
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (!line.trim()) continue;
      let msg; try { msg = JSON.parse(line); } catch { continue; }
      if (msg.type === 'ready') continue; // we already readied in the lobby
      if (ws && ws.readyState === 1) ws.send(line);
    }
  });
  bot.on('exit', () => { bot = null; });
}
function connect() {
  const u = new URL(`wss://play.joinbuilders.org/r/${room}/play`);
  u.searchParams.set('token', ''); u.searchParams.set('sid', sid); u.searchParams.set('name', NAME);
  ws = new WebSocket(u);
  ws.onopen = () => console.error('connected room', room);
  ws.onclose = (e) => { console.error('closed', e.code, e.reason); if (played < Number(matches)) setTimeout(connect, 3000); else process.exit(0); };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    let m; try { m = JSON.parse(String(e.data)); } catch { return; }
    if (m.type === 'lobby') {
      if (!inMatch && !(m.you && m.you.ready)) ws.send(JSON.stringify({ type: 'ready', name: NAME }));
      return;
    }
    if (m.type === 'hello') {
      inMatch = true; played++;
      if (m.map) m.map.dir = process.env.MAP_DIR || m.map.dir;
      console.error(`match ${played}: hello slot ${m.slot}`);
      log = fs.createWriteStream(`${outdir}/m${played}.jsonl`);
      startBot(played);
      ws.send(JSON.stringify({ type: 'loaded' }));
    }
    if (log) log.write(JSON.stringify(m) + '\n');
    if (bot && (m.type === 'hello' || m.type === 'obs' || m.type === 'end')) bot.stdin.write(JSON.stringify(m) + '\n');
    if (m.type === 'end') console.error(`match ${played}: end place ${m.place} kills ${m.kills}`);
    if (m.type === 'results' || m.type === 'end') {
      if (m.type === 'results') { inMatch = false; if (log) { log.end(); log = null; } if (bot) { try { bot.stdin.end(); } catch {} }
        if (played >= Number(matches)) { ws.close(); process.exit(0); } }
    }
  };
}
connect();
