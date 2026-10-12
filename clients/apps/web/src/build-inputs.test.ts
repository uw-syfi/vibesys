import {describe, expect, test} from 'bun:test';
import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {join, relative, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {WEB_BUNDLE_PACKAGES, webBuildInputs} from '../build-inputs.js';

const workspaceRoot = fileURLToPath(new URL('../../../../', import.meta.url));
const clientsRoot = join(workspaceRoot, 'clients');

interface Manifest {
  readonly name: string;
  readonly dependencies?: Record<string, string>;
}

function manifest(directory: string): Manifest {
  return JSON.parse(readFileSync(join(clientsRoot, directory, 'package.json'), 'utf8')) as Manifest;
}

/** Every workspace package directory (`pnpm-workspace.yaml`: `tui`, `libs/*`, `features/*`, `apps/*`). */
function workspaceDirectories(): string[] {
  return ['', 'libs/', 'features/', 'apps/'].flatMap(prefix =>
    readdirSync(join(clientsRoot, prefix), {withFileTypes: true})
      .filter(entry => entry.isDirectory())
      .map(entry => `${prefix}${entry.name}`)
      .filter(directory => existsSync(join(clientsRoot, directory, 'package.json'))),
  );
}

/** The app and every workspace package its production dependencies reach. */
function bundledPackages(): string[] {
  const byName = new Map(
    workspaceDirectories().map(directory => [manifest(directory).name, directory]),
  );
  const reached = new Set<string>();
  const pending = ['apps/web'];
  for (let directory = pending.pop(); directory !== undefined; directory = pending.pop()) {
    if (reached.has(directory)) continue;
    reached.add(directory);
    for (const name of Object.keys(manifest(directory).dependencies ?? {})) {
      const dependency = byName.get(name);
      if (dependency !== undefined) pending.push(dependency);
    }
  }
  return [...reached].sort();
}

describe('webBuildInputs', () => {
  test('names every workspace package the bundle compiles from source', () => {
    expect<string[]>([...WEB_BUNDLE_PACKAGES].sort()).toEqual(bundledPackages());
  });

  test('inventories every tracked input to the production bundle', () => {
    const inputs = webBuildInputs()
      .map(path => relative(workspaceRoot, resolve(path)).split(sep).join('/'))
      .sort();

    expect(inputs).toEqual(
      [
        ...WEB_BUNDLE_PACKAGES.flatMap(directory => [
          `clients/${directory}/package.json`,
          `clients/${directory}/src`,
          `clients/${directory}/tsconfig.json`,
        ]),
        'clients/apps/web/build-inputs.ts',
        'clients/apps/web/build-manifest.ts',
        'clients/apps/web/index.html',
        'clients/apps/web/tsconfig.lib.json',
        'clients/apps/web/vite.config.ts',
        'clients/package.json',
        'clients/pnpm-lock.yaml',
        'clients/pnpm-workspace.yaml',
        'clients/tools/tsconfig.base.json',
      ].sort(),
    );
  });
});
