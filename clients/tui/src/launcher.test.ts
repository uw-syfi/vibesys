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
const savedSetupEntrypoint = process.env['VIBESYS_SETUP_ENTRYPOINT'];
const savedTermFile = process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];
const savedArgsFile = process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'];

afterEach(async () => {
  if (savedPython === undefined) delete process.env['VIBESYS_PYTHON'];
  else process.env['VIBESYS_PYTHON'] = savedPython;
  if (savedRuntime === undefined) delete process.env['VIBESYS_TUI_RUNTIME'];
  else process.env['VIBESYS_TUI_RUNTIME'] = savedRuntime;
  if (savedEntrypoint === undefined) delete process.env['VIBESYS_TUI_ENTRYPOINT'];
  else process.env['VIBESYS_TUI_ENTRYPOINT'] = savedEntrypoint;
  if (savedSetupEntrypoint === undefined) delete process.env['VIBESYS_SETUP_ENTRYPOINT'];
  else process.env['VIBESYS_SETUP_ENTRYPOINT'] = savedSetupEntrypoint;
  if (savedTermFile === undefined) delete process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];
  else process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = savedTermFile;
  if (savedArgsFile === undefined) delete process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'];
  else process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'] = savedArgsFile;
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

  it('runs validation directly without starting the interactive client', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backend = await writeExecutable(
      'fake-backend.mjs',
      `
process.exit(
  process.argv.includes('validate') && process.argv.includes('examples/kv-store') ? 0 : 9,
);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = join(tempDir, 'missing-runtime');

    await expect(launch(['validate', 'examples/kv-store'])).resolves.toBe(0);
  });

  it('runs configured repository setup before starting the backend', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const backendArgs = join(tempDir, 'backend-args.json');
    const backend = await writeExecutable(
      'setup-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

if (process.argv.includes('tui-defaults')) {
  console.log(JSON.stringify({
    input_path: '/repo/examples/queue-spsc',
    experiment_name: 'queue-spsc-generated',
    repository_owner: 'vibesys-playground',
    repository_name: 'queue-spsc-generated',
    visibility: 'private',
  }));
  process.exit(0);
}
writeFileSync(process.env.VIBESYS_FAKE_BACKEND_ARGS_FILE, JSON.stringify(process.argv.slice(2)));
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(socket => {
  socket.once('data', data => {
    const request = JSON.parse(data.toString().split('\\n')[0]);
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
    const setup = await writeExecutable(
      'fake-setup.mjs',
      `
import {writeFileSync} from 'node:fs';
const defaults = JSON.parse(process.env.VIBESYS_SETUP_DEFAULTS);
writeFileSync(process.env.VIBESYS_SETUP_RESULT, JSON.stringify({
  inputPath: defaults.input_path,
  experimentName: defaults.experiment_name,
  repositoryOwner: defaults.repository_owner,
  repositoryName: defaults.repository_name,
  visibility: defaults.visibility,
}));
`,
    );
    const frontend = await writeExecutable('setup-frontend.mjs', 'process.exit(0);');

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;
    process.env['VIBESYS_SETUP_ENTRYPOINT'] = setup;
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = backendTerminated;
    process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'] = backendArgs;

    await expect(launch(['--input', 'examples/queue-spsc'])).resolves.toBe(0);
    const args = JSON.parse(await readFile(backendArgs, 'utf8')) as string[];
    expect(args).toContain('vibesys-playground/queue-spsc-generated');
    expect(args).toContain('queue-spsc-generated');
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
