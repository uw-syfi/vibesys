import {fileURLToPath} from 'node:url';

/** Repository inputs that can change the gateway's production browser bundle. */
export function webBuildInputs(): string[] {
  return [
    './src',
    './index.html',
    './vite.config.ts',
    './build-inputs.ts',
    './build-manifest.ts',
    './workspace-source-aliases.ts',
    './workspace-source-aliases.json',
    './package.json',
    './tsconfig.json',
    '../backend-client/src',
    '../backend-client/package.json',
    '../backend-client/tsconfig.json',
    '../core-state/src',
    '../core-state/package.json',
    '../core-state/tsconfig.json',
    '../package.json',
    '../pnpm-lock.yaml',
    '../pnpm-workspace.yaml',
  ].map(path => fileURLToPath(new URL(path, import.meta.url)));
}
