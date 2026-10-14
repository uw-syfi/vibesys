import {fileURLToPath} from 'node:url';

/**
 * The workspace packages the production bundle compiles from source, relative to `clients/`: this
 * app's workspace dependencies and theirs. `build-inputs.test.ts` holds the list to the manifests.
 */
export const WEB_BUNDLE_PACKAGES = [
  'apps/web',
  'libs/contracts',
  'features/gateway-connect',
  'features/campaign',
  'features/conversation',
  'features/run-view',
  'libs/hosts',
  'libs/ipc',
  'libs/machine',
  'libs/run-session',
  'libs/run-state',
  'libs/runs',
  'libs/store',
  'libs/transport',
  'libs/ui',
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
    'apps/web/campaign-stream.ts',
    'apps/web/dev/fixtures/trajectory-replay.json',
    'apps/web/build-inputs.ts',
    'apps/web/build-manifest.ts',
    'apps/web/tsconfig.lib.json',
    'package.json',
    'tools/tsconfig.base.json',
    'pnpm-lock.yaml',
    'pnpm-workspace.yaml',
  ].map(path => fileURLToPath(new URL(`../../${path}`, import.meta.url)));
}
