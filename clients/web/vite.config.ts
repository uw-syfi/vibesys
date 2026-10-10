import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig, type Plugin} from 'vite';
import {webBuildManifest} from './build-manifest.js';
import {workspaceSourceAliases} from './workspace-source-aliases.js';

const workspaceRoot = fileURLToPath(new URL('../../', import.meta.url));

function replayFixturePlugin(): Plugin {
  const fixture = fileURLToPath(
    new URL('../tui/dev/fixtures/framework-events.jsonl', import.meta.url),
  );
  return {
    name: 'vibesys-replay-fixture',
    configureServer(server) {
      server.middlewares.use(
        '/__vibesys/fixtures/framework-events.jsonl',
        async (_request, response) => {
          response.setHeader('Content-Type', 'application/x-ndjson');
          response.end(await readFile(fixture, 'utf8'));
        },
      );
    },
  };
}

export default defineConfig({
  plugins: [
    react(),
    replayFixturePlugin(),
    webBuildManifest({
      workspaceRoot,
      sourceRoots: [
        fileURLToPath(new URL('./src', import.meta.url)),
        fileURLToPath(new URL('../backend-client/src', import.meta.url)),
        fileURLToPath(new URL('../core-state/src', import.meta.url)),
      ],
    }),
  ],
  resolve: {
    alias: workspaceSourceAliases(),
  },
});
