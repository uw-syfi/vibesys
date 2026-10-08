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

function clampInterval(raw: string | null): number {
  const value = Number(raw);
  if (!Number.isFinite(value)) return 350;
  return Math.min(5000, Math.max(20, value));
}

/**
 * Dev-only replay-as-live server. Loads the committed campaign record, converts
 * it to the frame stream a live run would emit (via the app's own
 * `framesFromRecord`, so there is no second producer), and pushes frames over
 * Server-Sent Events on a timer. `?interval=<ms>` paces playback. See
 * docs/design/live-campaign-streaming.md.
 */
function campaignStreamPlugin(): Plugin {
  const recordPath = fileURLToPath(
    new URL('./dev/fixtures/trajectory-replay.json', import.meta.url),
  );
  return {
    name: 'vibesys-campaign-stream',
    configureServer(server) {
      server.middlewares.use('/__vibesys/campaign/stream', async (request, response) => {
        let frames: unknown[];
        try {
          const [{parseReplayScenario}, {framesFromRecord}] = await Promise.all([
            server.ssrLoadModule('/src/replay-scenario.ts'),
            server.ssrLoadModule('/src/campaign-replay.ts'),
          ]);
          const record = parseReplayScenario(JSON.parse(await readFile(recordPath, 'utf8')));
          frames = framesFromRecord(record);
        } catch (error) {
          response.statusCode = 500;
          response.end(error instanceof Error ? error.message : String(error));
          return;
        }

        const interval = clampInterval(
          new URL(request.url ?? '', 'http://localhost').searchParams.get('interval'),
        );
        response.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache, no-transform',
          Connection: 'keep-alive',
        });

        let index = 0;
        const timer = setInterval(() => {
          if (index >= frames.length) {
            clearInterval(timer);
            // Leave the connection open and idle: the run has completed, so the
            // browser's EventSource should not reconnect and replay.
            return;
          }
          response.write(`data: ${JSON.stringify(frames[index])}\n\n`);
          index += 1;
        }, interval);
        request.on('close', () => clearInterval(timer));
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), replayFixturePlugin(), campaignStreamPlugin()],
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
