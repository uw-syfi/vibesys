#!/usr/bin/env node
// Copy the two page bundles into `dist/ui`, where the main process serves them from
// `app://vibesys`: the run view (`@vibesys/web`'s `build:desktop` output, `desktop.html`) and the
// app's own welcome view (`vite.welcome.config.ts`'s output, `welcome.html` and `welcome-assets/`).
// The app consumes the web package only as these built files, never as an import.
import {cpSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const bundles = [
  {source: fileURLToPath(new URL('../../web/.desktop-dist/', import.meta.url)), page: 'desktop.html', build: 'pnpm --filter @vibesys/web build:desktop'},
  {source: fileURLToPath(new URL('../.welcome-dist/', import.meta.url)), page: 'welcome.html', build: 'vite build --config vite.welcome.config.ts'},
];
const target = fileURLToPath(new URL('../dist/ui/', import.meta.url));

for (const {source, page, build} of bundles) {
  if (!existsSync(join(source, page))) {
    console.error(`copy-ui: ${source} has no ${page}; run ${build}`);
    process.exit(1);
  }
  cpSync(source, target, {recursive: true});
}
