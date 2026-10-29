#!/usr/bin/env node
// Copy the app shell's bundle (`@vibesys/app-desktop-shell`'s build: `shell.html` and
// `shell-assets/`) into `dist/ui`, where the main process serves it from `app://vibesys`.
// The main process consumes the renderer app only as these built files, never as an import.
import {cpSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const source = fileURLToPath(new URL('../../desktop-shell/dist/', import.meta.url));
const target = fileURLToPath(new URL('../dist/ui/', import.meta.url));

if (!existsSync(join(source, 'shell.html'))) {
  console.error(`copy-ui: ${source} has no shell.html; run pnpm --filter @vibesys/app-desktop-shell build`);
  process.exit(1);
}
cpSync(source, target, {recursive: true});
