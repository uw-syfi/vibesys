#!/usr/bin/env node
// Copy the web UI's desktop bundle (`@vibesys/web`'s `build:desktop` output) into `dist/ui`, where
// the main process serves it from `app://vibesys`. The app consumes the web package only as these
// built files, never as an import.
import {cpSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const source = fileURLToPath(new URL('../../web/.desktop-dist/', import.meta.url));
const target = fileURLToPath(new URL('../dist/ui/', import.meta.url));

if (!existsSync(join(source, 'desktop.html'))) {
  console.error(`copy-ui: ${source} has no desktop.html; run pnpm --filter @vibesys/web build:desktop`);
  process.exit(1);
}
cpSync(source, target, {recursive: true});
