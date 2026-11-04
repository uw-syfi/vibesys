#!/usr/bin/env node

/**
 * Serves the built gallery (`dist/`) on 127.0.0.1 at the port given as the one argument. Plain Node
 * with no dependency, so the suite runs the same in CI and in the Linux image that writes the
 * screenshot baselines, where the host's native bundler binaries would not load.
 */

import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {extname, join, normalize} from 'node:path';
import {fileURLToPath} from 'node:url';

const ROOT = fileURLToPath(new URL('../dist/', import.meta.url));
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.woff2': 'font/woff2',
  '.svg': 'image/svg+xml',
};

const port = Number(process.argv[2]);
if (!Number.isInteger(port)) throw new Error('usage: serve-dist.mjs <port>');

createServer(async (request, response) => {
  const path = new URL(request.url ?? '/', 'http://localhost').pathname;
  const file = normalize(join(ROOT, path.endsWith('/') ? `${path}index.html` : path));
  if (!file.startsWith(ROOT)) {
    response.writeHead(403).end();
    return;
  }
  try {
    const body = await readFile(file);
    response.writeHead(200, {'content-type': TYPES[extname(file)] ?? 'application/octet-stream'});
    response.end(body);
  } catch {
    response.writeHead(404).end();
  }
}).listen(port, '127.0.0.1');
