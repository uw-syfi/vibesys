#!/usr/bin/env node

import {readFile} from 'node:fs/promises';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
let bundle;
for (const name of ['browser-entry.js', 'browser-entry.mjs']) {
  try {
    bundle = await readFile(join(root, 'web', '.browser-dist', name), 'utf8');
    break;
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
}
if (bundle === undefined) {
  throw new Error('Browser bundle was not built at web/.browser-dist/browser-entry.js or .mjs');
}
const forbidden = [...bundle.matchAll(/(?:node:|node_modules\/node:)[^'"\s]*/g)].map(
  match => match[0],
);
if (forbidden.length > 0) {
  console.error(`Browser bundle contains Node builtins: ${[...new Set(forbidden)].join(', ')}`);
  process.exitCode = 1;
} else {
  console.log('Browser transport bundle contains no Node builtin imports.');
}
