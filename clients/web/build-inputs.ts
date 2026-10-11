import {fileURLToPath} from 'node:url';

/** Repository inputs that can change the gateway's production browser bundle. */
export function webBuildInputs(): string[] {
  return [
    './src',
    './index.html',
    './vite.config.ts',
    './build-inputs.ts',
    './build-manifest.ts',
    './package.json',
    './tsconfig.json',
    './tsconfig.app.json',
    '../transport/src',
    '../transport/package.json',
    '../transport/tsconfig.json',
    '../contracts/src',
    '../contracts/package.json',
    '../contracts/tsconfig.json',
    '../core-state/src',
    '../core-state/package.json',
    '../core-state/tsconfig.json',
    '../package.json',
    '../tsconfig.base.json',
    '../pnpm-lock.yaml',
    '../pnpm-workspace.yaml',
  ].map(path => fileURLToPath(new URL(path, import.meta.url)));
}
