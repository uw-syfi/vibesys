import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import react from '@vitejs/plugin-react';
import {defineConfig, type Plugin} from 'vite';
import {workspaceSourceAliases} from './workspace-source-aliases.js';

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
    alias: workspaceSourceAliases(),
  },
});
