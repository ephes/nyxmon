import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

export const fixtures = JSON.parse(execFileSync('uv', ['run', 'python', 'tests/browser/render_fixtures.py'], { encoding: 'utf8' }));
const assets = resolve('src/nyxboard/static');
export async function serve() {
  const server = createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    let body;
    if (url.pathname === '/') { res.setHeader('Content-Type', 'text/html'); body = fixtures.html; }
    else if (url.pathname === fixtures.htmxUrl) { res.setHeader('Content-Type', 'text/javascript'); body = fixtures.htmx; }
    else if (/^\/static\/(js|css)\/[\w./-]+$/.test(url.pathname) && !url.pathname.includes('..')) {
      try { body = readFileSync(join(assets, url.pathname.slice('/static/'.length))); }
      catch { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', url.pathname.endsWith('.js') ? 'text/javascript' : 'text/css');
    } else if (url.pathname === '/set-theme/') { body = '{}'; }
    else { res.writeHead(404).end(); return; }
    res.end(body);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(resolve => server.close(resolve)) };
}
