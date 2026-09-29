import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig, type Plugin} from 'vite';

function replayFixturePlugin(): Plugin {
  const fixture = fileURLToPath(new URL('./src/fixtures/demo-run.jsonl', import.meta.url));
  return {
    name: 'vibesys-replay-fixture',
    configureServer(server) {
      server.middlewares.use('/__vibesys/fixtures/demo-run.jsonl', async (_request, response) => {
        response.setHeader('Content-Type', 'application/x-ndjson');
        response.end(await readFile(fixture, 'utf8'));
      });
    },
  };
}

/**
 * The home server (sub-project 2) listens here; `vibesys web home --port` changes it.
 * An empty VIBESYS_HOME_PORT counts as unset, as in the desktop shell's launch policy.
 */
const HOME_PORT = process.env['VIBESYS_HOME_PORT'] || '8764';

export default defineConfig({
  plugins: [react(), replayFixturePlugin()],
  server: {
    // The app calls the home API same-origin; in dev Vite forwards it. `changeOrigin` rewrites
    // Host for the home server's checks and leaves the browser's Origin as it is.
    proxy: {'/api': {target: `http://127.0.0.1:${HOME_PORT}`, changeOrigin: true}},
  },
  resolve: {
    alias: [
      {
        find: '@vibesys/backend-client/websocket',
        replacement: fileURLToPath(new URL('../backend-client/src/websocket.ts', import.meta.url)),
      },
      {
        find: '@vibesys/backend-client',
        replacement: fileURLToPath(new URL('../backend-client/src/index.ts', import.meta.url)),
      },
      {
        find: '@vibesys/core-state',
        replacement: fileURLToPath(new URL('../core-state/src/index.ts', import.meta.url)),
      },
    ],
  },
});
