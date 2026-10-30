// Keep the old standalone server alive until the next immutable release is healthy.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

const releases = process.env.LYPH_RELEASES_DIR || '/ui/releases';
const baseline = process.env.LYPH_BASELINE_DIR || '/app';
let active = null;
let switching = false;
const children = new Set();
function sourceFor(id) {
  if (id === 'baseline') return baseline;
  if (!/^[a-f0-9]{32}$/.test(id)) throw new Error('Invalid release identifier');
  return path.join(releases, id);
}
async function boot(id) {
  // OS assigns a fresh port; a retiring child cannot satisfy a new child's probe.
  const reserve = http.createServer();
  await new Promise(resolve => reserve.listen(0, '127.0.0.1', resolve));
  const port = reserve.address().port;
  await new Promise(resolve => reserve.close(resolve));
  const directory = sourceFor(id);
  const child = spawn(process.execPath, [path.join(directory, 'server.js')], {
    cwd: directory, env: { ...process.env, HOSTNAME: '127.0.0.1', PORT: String(port), NODE_ENV: 'production' }, stdio: ['ignore', 'inherit', 'inherit']
  });
  children.add(child);
  child.once('exit', () => children.delete(child));
  let spawnError = null;
  child.once('error', error => { spawnError = error; children.delete(child); });
  let ready = false;
  for (let attempt = 0; attempt < 60; attempt++) {
    if (child.exitCode !== null || spawnError) break;
    try {
      const response = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(2000) });
      await response.body?.cancel();
      if (response.status < 500) { ready = true; break; }
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  if (!ready) { child.kill('SIGTERM'); throw new Error('Release failed readiness'); }
  const previous = active;
  active = { id, port, child };
  if (previous) setTimeout(() => previous.child.kill('SIGTERM'), 3000).unref();
}
await boot('baseline');
const server = http.createServer((request, response) => {
  if (request.url === '/_lyph/health') {
    response.writeHead(active?.child.exitCode === null ? 200 : 503, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    response.end(JSON.stringify({ active_version: active?.id, status: active?.child.exitCode === null ? 'ok' : 'unavailable' }));
    return;
  }
  if (!active || active.child.exitCode !== null) { response.writeHead(503); response.end('Service starting'); return; }
  const upstream = http.request({ hostname: '127.0.0.1', port: active.port, method: request.method, path: request.url, headers: request.headers }, reply => {
    response.writeHead(reply.statusCode || 502, reply.headers);
    reply.pipe(response);
  });
  upstream.on('error', () => { if (!response.headersSent) response.writeHead(502); response.end('Upstream unavailable'); });
  request.on('aborted', () => upstream.destroy());
  request.pipe(upstream);
});
server.listen(Number(process.env.PORT || 3000), '0.0.0.0');
setInterval(async () => {
  if (switching) return;
  switching = true;
  try {
    const pointer = JSON.parse(await readFile(path.join(releases, 'active.json'), 'utf8'));
    if (pointer.id !== active?.id) await boot(pointer.id);
  } catch (error) {
    if (error.code !== 'ENOENT') console.error('Release switch rejected:', error.message);
  } finally { switching = false; }
}, 2000).unref();
function stop() {
  server.close();
  for (const child of children) child.kill('SIGTERM');
  setTimeout(() => process.exit(0), 3000).unref();
}
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
