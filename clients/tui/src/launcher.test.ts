import {access, chmod, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {afterEach, describe, expect, it} from 'vitest';
import {launch} from './launcher.js';

let tempDir: string | undefined;
const savedPython = process.env['VIBESYS_PYTHON'];
const savedRuntime = process.env['VIBESYS_TUI_RUNTIME'];
const savedEntrypoint = process.env['VIBESYS_TUI_ENTRYPOINT'];
const savedTermFile = process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];

afterEach(async () => {
  if (savedPython === undefined) delete process.env['VIBESYS_PYTHON'];
  else process.env['VIBESYS_PYTHON'] = savedPython;
  if (savedRuntime === undefined) delete process.env['VIBESYS_TUI_RUNTIME'];
  else process.env['VIBESYS_TUI_RUNTIME'] = savedRuntime;
  if (savedEntrypoint === undefined) delete process.env['VIBESYS_TUI_ENTRYPOINT'];
  else process.env['VIBESYS_TUI_ENTRYPOINT'] = savedEntrypoint;
  if (savedTermFile === undefined) delete process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];
  else process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = savedTermFile;
  if (tempDir) await rm(tempDir, {recursive: true, force: true});
  tempDir = undefined;
});

describe('launcher', () => {
  it('publishes simple installed command names', async () => {
    const packageJsonPath = join(dirname(fileURLToPath(import.meta.url)), '..', 'package.json');
    const packageJson = JSON.parse(await readFile(packageJsonPath, 'utf8')) as {
      bin?: Record<string, string>;
    };

    expect(packageJson.bin).toEqual({
      vibesys: './dist/launcher.js',
      vs: './dist/launcher.js',
    });
  });

  it('starts a headless backend, waits for readiness, and runs the frontend', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const backend = await writeExecutable(
      'fake-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(socket => {
  let buffer = '';
  socket.setEncoding('utf8');
  socket.on('data', chunk => {
    buffer += chunk;
    if (!buffer.includes('\\n')) return;
    const request = JSON.parse(buffer.split('\\n')[0]);
    socket.end(JSON.stringify({
      protocol_version: 1,
      request_id: request.request_id,
      timestamp: new Date().toISOString(),
      ok: true,
      events: [],
    }) + '\\n');
  });
});
server.listen(socketPath);
process.on('SIGTERM', () => {
  writeFileSync(process.env.VIBESYS_FAKE_BACKEND_TERM_FILE, 'terminated');
  server.close(() => process.exit(0));
});
`,
    );
    const frontend = await writeExecutable(
      'fake-frontend.mjs',
      `
if (!process.env.VIBESYS_CONTROL_SOCKET) process.exit(7);
process.exit(0);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = frontend;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = backendTerminated;

    await expect(launch(['--stub-agent'])).resolves.toBe(0);
    await access(backendTerminated);
  });
});

async function writeExecutable(name: string, source: string): Promise<string> {
  if (!tempDir) throw new Error('tempDir is required');
  const path = join(tempDir, name);
  await writeFile(path, `#!/usr/bin/env node\n${source}`);
  await chmod(path, 0o755);
  return path;
}
