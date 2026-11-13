import {fileURLToPath} from 'node:url';
import type {KnipConfig} from 'knip';
import {workspaceLayout} from './scripts/workspace_layout.mjs';

type WorkspacePolicy = NonNullable<KnipConfig['workspaces']>[string];

// What knip audits inside each package. The keys are checked against the workspace layout below,
// not maintained here: a package that joins the workspace without a policy fails this gate by
// name instead of silently falling back to knip's default entry-point heuristics, which is how
// `web` came to be audited against defaults meant for a library rather than a bundled app.
const PACKAGE_POLICIES: Record<string, WorkspacePolicy> = {
  'libs/proto': {
    entry: ['src/index.ts', 'tests/*.mjs'],
    project: ['src/**/*.ts', '*.mjs', 'tests/*.mjs'],
    // Tool-owned bindings are guarded by the proto regenerate-and-diff check.
    ignore: ['src/generated/**'],
  },
  'libs/orchestrator-proto': {
    entry: ['src/index.ts', 'src/**/*.test.ts'],
    project: ['src/**/*.ts'],
    // Tool-owned bindings, regenerated and diffed by the proto check.
    ignore: ['src/generated/**'],
  },
  'apps/desktop': {
    // The Electron main process and preload, the renderer page (`index.html`, built by
    // `vite.config.ts`), and the Playwright journey that drives the built app.
    entry: [
      'src/main/index.ts',
      'src/preload/index.cts',
      'src/ui/main.tsx',
      // The browser page over a fake daemon (`fixture.html`, built with `vite build --mode fixture`).
      'src/testing/page/main.tsx',
      'src/**/*.test.ts',
      'src/**/*.test.tsx',
      // The journey the desktop path test (`gateway/e2e`) runs against the built app.
      'e2e/observe-run.ts',
      // The app-window contact-sheet capture suite and the static server it starts by name
      // (`playwright.fixture.config.ts`), which knip cannot see through the string command.
      'e2e/fixture/*.spec.ts',
      'e2e/fixture/serve-dist.mjs',
    ],
    project: [
      'src/**/*.ts',
      'src/**/*.cts',
      'src/**/*.tsx',
      'e2e/**/*.{ts,mjs}',
      'playwright.config.ts',
    ],
  },
  'apps/ui-gallery': {
    // The development-only component gallery (`index.html`, built by `vite.config.ts`), its
    // Playwright suite, and the static server that suite starts (`playwright.config.ts`).
    entry: ['src/main.tsx', 'src/**/*.test.ts', 'e2e/**/*.spec.ts', 'e2e/serve-dist.mjs'],
    project: ['src/**/*.ts', 'src/**/*.tsx', 'e2e/**/*.{ts,mjs}'],
  },
};

// Repository tooling run by `node tools/scripts/...`; `*.test.mjs` runs under `node --test`. The
// workspace root is not a package, so it is not part of the derived set.
const ROOT_POLICY: WorkspacePolicy = {
  // Invoked by root scripts/generate_proto.sh through buf.gen.yaml.
  ignoreDependencies: ['@bufbuild/protoc-gen-es'],
  entry: ['tools/scripts/*.mjs'],
  project: ['tools/scripts/**/*.mjs'],
};

const layout = workspaceLayout(fileURLToPath(new URL('..', import.meta.url)));

const workspaces: Record<string, WorkspacePolicy> = {'.': ROOT_POLICY};
// A package on the per-entry layout (every package laid out like `libs/display-names`) needs no policy
// here: its entries are the sources its exports name, plus its tests.
function derivedPolicy(manifest: {exports?: Record<string, Record<string, string>>}) {
  const sources = Object.values(manifest.exports ?? {}).flatMap(target => {
    const source = target['@vibesys/source'];
    return source === undefined ? [] : [source.replace(/^\.\//, '')];
  });
  if (sources.length === 0) return undefined;
  return {
    entry: [...sources, 'src/**/*.test.ts', 'src/**/*.test.tsx'],
    project: ['src/**/*.ts', 'src/**/*.tsx'],
  };
}

for (const {name, directory, manifest} of layout.packages) {
  const policy = PACKAGE_POLICIES[directory] ?? derivedPolicy(manifest);
  if (policy === undefined) {
    throw new Error(`knip.config.ts: ${name} (${directory}/) has no entry or project policy`);
  }
  workspaces[directory] = policy;
}

const config: KnipConfig = {
  // The protocol generator calls an external Python tool.
  // Go is a system build tool, like uv; gateway builds report its absence explicitly.
  ignoreBinaries: ['uv', 'go'],
  workspaces,
};

export default config;
