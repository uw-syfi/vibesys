#!/usr/bin/env node
// Copy the two page bundles into `dist/ui`, where the main process serves them from
// `app://vibesys`: the run view (`@vibesys/app-web`'s `build:desktop` output, `desktop.html`) and the
// welcome view (`@vibesys/app-desktop-renderer`'s build, `welcome.html` and `welcome-assets/`).
// The main process consumes the renderer apps only as these built files, never as an import.
import {cpSync, existsSync} from 'node:fs';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const bundles = [
  {source: fileURLToPath(new URL('../../web/.desktop-dist/', import.meta.url)), page: 'desktop.html', build: 'pnpm --filter @vibesys/app-web build:desktop'},
  {source: fileURLToPath(new URL('../../desktop-renderer/dist/', import.meta.url)), page: 'welcome.html', build: 'pnpm --filter @vibesys/app-desktop-renderer build'},
];
const target = fileURLToPath(new URL('../dist/ui/', import.meta.url));

for (const {source, page, build} of bundles) {
  if (!existsSync(join(source, page))) {
    console.error(`copy-ui: ${source} has no ${page}; run ${build}`);
    process.exit(1);
  }
  cpSync(source, target, {recursive: true});
}
