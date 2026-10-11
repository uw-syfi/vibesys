import {fileURLToPath} from 'node:url';

/**
 * The workspace packages the production bundle compiles from source, relative to `clients/`: this
 * app's workspace dependencies and theirs. `build-inputs.test.ts` holds the list to the manifests.
 */
export const WEB_BUNDLE_PACKAGES = [
  'apps/web',
  'contracts',
  'core-state',
  'entities',
  'features/gateway-connect',
  'features/replay',
  'features/run-view',
  'ipc',
  'transport',
  'ui',
] as const;

/** Repository inputs that can change the gateway's production browser bundle. */
export function webBuildInputs(): string[] {
  const packages = WEB_BUNDLE_PACKAGES.flatMap(directory => [
    `${directory}/src`,
    `${directory}/package.json`,
    `${directory}/tsconfig.json`,
  ]);
  return [
    ...packages,
    'apps/web/index.html',
    'apps/web/vite.config.ts',
    'apps/web/build-inputs.ts',
    'apps/web/build-manifest.ts',
    'apps/web/tsconfig.lib.json',
    'package.json',
    'tsconfig.base.json',
    'pnpm-lock.yaml',
    'pnpm-workspace.yaml',
  ].map(path => fileURLToPath(new URL(`../../${path}`, import.meta.url)));
}
