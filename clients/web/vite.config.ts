import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig, type Plugin} from 'vite';

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
  plugins: [react(), replayFixturePlugin()],
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
