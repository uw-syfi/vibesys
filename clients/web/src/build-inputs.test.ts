import {describe, expect, test} from 'bun:test';
import {relative, resolve, sep} from 'node:path';
import {fileURLToPath} from 'node:url';
import {webBuildInputs} from '../build-inputs.js';

const workspaceRoot = fileURLToPath(new URL('../../../', import.meta.url));

describe('webBuildInputs', () => {
  test('inventories every tracked input to the production bundle', () => {
    const inputs = webBuildInputs()
      .map(path => relative(workspaceRoot, resolve(path)).split(sep).join('/'))
      .sort();

    expect(inputs).toEqual(
      [
        'clients/backend-client/package.json',
        'clients/backend-client/src',
        'clients/backend-client/tsconfig.json',
        'clients/core-state/package.json',
        'clients/core-state/src',
        'clients/core-state/tsconfig.json',
        'clients/package.json',
        'clients/pnpm-lock.yaml',
        'clients/pnpm-workspace.yaml',
        'clients/web/build-inputs.ts',
        'clients/web/build-manifest.ts',
        'clients/web/index.html',
        'clients/web/package.json',
        'clients/web/src',
        'clients/web/tsconfig.json',
        'clients/web/vite.config.ts',
        'clients/web/workspace-source-aliases.json',
        'clients/web/workspace-source-aliases.ts',
      ].sort(),
    );
  });
});
