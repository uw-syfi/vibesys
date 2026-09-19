import {afterEach, describe, expect, it} from 'bun:test';
import {access, chmod, mkdtemp, readFile, realpath, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {launch} from './launcher.js';

let tempDir: string | undefined;
const savedPython = process.env['VIBESYS_PYTHON'];
const savedRuntime = process.env['VIBESYS_TUI_RUNTIME'];
const savedEntrypoint = process.env['VIBESYS_TUI_ENTRYPOINT'];
const savedTermFile = process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];
const savedArgsFile = process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'];
const savedReleaseSmokeMarker = process.env['VIBESYS_RELEASE_SMOKE_MARKER'];
const originalCwd = process.cwd();

afterEach(async () => {
  process.chdir(originalCwd);
  if (savedPython === undefined) delete process.env['VIBESYS_PYTHON'];
  else process.env['VIBESYS_PYTHON'] = savedPython;
  if (savedRuntime === undefined) delete process.env['VIBESYS_TUI_RUNTIME'];
  else process.env['VIBESYS_TUI_RUNTIME'] = savedRuntime;
  if (savedEntrypoint === undefined) delete process.env['VIBESYS_TUI_ENTRYPOINT'];
  else process.env['VIBESYS_TUI_ENTRYPOINT'] = savedEntrypoint;
  if (savedTermFile === undefined) delete process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'];
  else process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = savedTermFile;
  if (savedArgsFile === undefined) delete process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'];
  else process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'] = savedArgsFile;
  if (savedReleaseSmokeMarker === undefined) delete process.env['VIBESYS_RELEASE_SMOKE_MARKER'];
  else process.env['VIBESYS_RELEASE_SMOKE_MARKER'] = savedReleaseSmokeMarker;
  delete process.env['VIBESYS_FAKE_FRONTEND_THEME_FILE'];
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

  it('starts the frontend while the backend is still coming up', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const defaultsInvoked = join(tempDir, 'defaults-invoked');
    const socketSeen = join(tempDir, 'socket-seen');
    const backend = await writeExecutable(
      'slow-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

if (process.argv.includes('tui-defaults')) {
  writeFileSync(${JSON.stringify(defaultsInvoked)}, 'invoked');
  console.log(JSON.stringify({theme: 'dark'}));
  process.exit(0);
}
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(socket => socket.end());
setTimeout(() => server.listen(socketPath), 300);
process.on('SIGTERM', () => {
  writeFileSync(process.env.VIBESYS_FAKE_BACKEND_TERM_FILE, 'terminated');
  process.exit(0);
});
`,
    );
    const frontend = await writeExecutable(
      'probe-frontend.mjs',
      `
import {existsSync, writeFileSync} from 'node:fs';
writeFileSync(
  ${JSON.stringify(socketSeen)},
  existsSync(process.env.VIBESYS_CONTROL_SOCKET) ? 'listening' : 'starting',
);
process.exit(0);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = backendTerminated;

    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(0);
    // The frontend owns the wait for the socket, so the launcher spawns it
    // before the backend listens and never interposes a defaults process.
    expect(await readFile(socketSeen, 'utf8')).toBe('starting');
    await expect(access(defaultsInvoked)).rejects.toThrow();
  }, 15_000);

  it('reports the backend log when the backend dies before it listens', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const frontendTerminated = join(tempDir, 'frontend-terminated');
    const backend = await writeExecutable(
      'doomed-backend.mjs',
      `
console.error('vibesys: configuration is unreadable');
// Give the concurrently spawned frontend time to install its signal handler,
// so the test observes the launcher's termination rather than an OS-level race.
setTimeout(() => process.exit(3), 250);
`,
    );
    const frontend = await writeExecutable(
      'waiting-frontend.mjs',
      `
import {writeFileSync} from 'node:fs';
process.on('SIGTERM', () => {
  writeFileSync(${JSON.stringify(frontendTerminated)}, 'terminated');
  process.exit(143);
});
setInterval(() => undefined, 1000);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    const errors: string[] = [];
    const originalError = console.error;
    console.error = (...args: unknown[]) => {
      errors.push(args.map(String).join(' '));
    };
    try {
      await expect(launch(['--stub-agent'])).resolves.toBe(3);
    } finally {
      console.error = originalError;
    }

    await access(frontendTerminated);
    expect(errors.join('\n')).toContain('backend exited with status 3');
    expect(errors.join('\n')).toContain('configuration is unreadable');
  }, 15_000);

  it('terminates a lingering frontend when the backend dies after it listens', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const frontendTerminated = join(tempDir, 'frontend-terminated');
    const backend = await writeExecutable(
      'dying-backend.mjs',
      `
import {createServer} from 'node:net';
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(() => {
  // Unexpected death after the client attached, with no terminal event.
  setTimeout(() => process.exit(7), 100);
});
server.listen(socketPath);
`,
    );
    const frontend = await writeExecutable(
      'lingering-frontend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createConnection} from 'node:net';
process.on('SIGTERM', () => {
  writeFileSync(${JSON.stringify(frontendTerminated)}, 'terminated');
  process.exit(143);
});
setInterval(() => undefined, 1000);
let connected = false;
function connect(path) {
  const socket = createConnection(path);
  socket.once('connect', () => {
    connected = true;
  });
  socket.on('error', () => {
    if (!connected) setTimeout(() => connect(path), 25);
  });
}
connect(process.env.VIBESYS_CONTROL_SOCKET);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(7);
    expect(await readFile(frontendTerminated, 'utf8')).toBe('terminated');
  }, 20_000);

  it('returns the frontend failure when it exits within the grace window after backend death', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backend = await writeExecutable(
      'dying-backend.mjs',
      `
import {createServer} from 'node:net';
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(() => {
  setTimeout(() => process.exit(7), 100);
});
server.listen(socketPath);
`,
    );
    const frontend = await writeExecutable(
      'drop-sensitive-frontend.mjs',
      `
import {createConnection} from 'node:net';
setInterval(() => undefined, 1000);
let connected = false;
function connect(path) {
  const socket = createConnection(path);
  socket.once('connect', () => {
    connected = true;
  });
  socket.once('close', () => {
    // Notice the dropped transport shortly after the backend dies, well
    // within the launcher's grace window but after it saw the backend exit.
    if (connected) setTimeout(() => process.exit(3), 300);
  });
  socket.on('error', () => {
    if (!connected) setTimeout(() => connect(path), 25);
  });
}
connect(process.env.VIBESYS_CONTROL_SOCKET);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(3);
  }, 15_000);

  it('pins the backend failure code when the frontend exits 0 within the grace window after backend death', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backend = await writeExecutable(
      'dying-backend.mjs',
      `
import {createServer} from 'node:net';
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(() => {
  setTimeout(() => process.exit(7), 100);
});
server.listen(socketPath);
`,
    );
    const frontend = await writeExecutable(
      'clean-exit-frontend.mjs',
      `
import {createConnection} from 'node:net';
setInterval(() => undefined, 1000);
let connected = false;
function connect(path) {
  const socket = createConnection(path);
  socket.once('connect', () => {
    connected = true;
  });
  socket.once('close', () => {
    // Exit as if the run had finished cleanly, well within the launcher's
    // grace window but after it saw the backend exit. The backend's
    // failure must still win: a frontend that happens to exit 0 here is
    // not evidence the run succeeded.
    if (connected) setTimeout(() => process.exit(0), 300);
  });
  socket.on('error', () => {
    if (!connected) setTimeout(() => connect(path), 25);
  });
}
connect(process.env.VIBESYS_CONTROL_SOCKET);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(7);
  }, 15_000);

  it('starts a headless backend and runs the frontend', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const backend = await writeExecutable(
      'fake-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

if (process.argv.includes('tui-defaults')) {
  console.log(JSON.stringify({theme: 'dark'}));
  process.exit(0);
}
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

    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(0);
    await access(backendTerminated);
  });

  it('makes a successful frontend authoritative only in release smoke mode', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const smokeMarker = join(tempDir, 'frontend-started');
    const backend = await writeExecutable(
      'race-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

if (process.argv.includes('tui-defaults')) {
  console.log(JSON.stringify({theme: 'dark'}));
  process.exit(0);
}
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(socket => socket.end());
server.listen(socketPath);
// The run fails on its own once the client has had time to attach.
setTimeout(() => process.exit(2), 1000);
process.on('SIGTERM', () => {
  writeFileSync(process.env.VIBESYS_FAKE_BACKEND_TERM_FILE, 'terminated');
  server.close(() => process.exit(0));
});
`,
    );
    const frontend = await writeExecutable(
      'smoke-frontend.mjs',
      `
import {writeFileSync} from 'node:fs';

if (!process.env.VIBESYS_CONTROL_SOCKET) process.exit(7);
if (process.env.VIBESYS_RELEASE_SMOKE_MARKER) {
  writeFileSync(process.env.VIBESYS_RELEASE_SMOKE_MARKER, 'started');
}
process.exit(0);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    delete process.env['VIBESYS_RELEASE_SMOKE_MARKER'];
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = join(tempDir, 'normal-backend-terminated');
    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(2);

    process.env['VIBESYS_RELEASE_SMOKE_MARKER'] = smokeMarker;
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = backendTerminated;
    await expect(launch(['--stub-agent', '--runs-dir', '/tmp/vibesys-test-runs'])).resolves.toBe(0);
    expect(await readFile(smokeMarker, 'utf8')).toBe('started');
    expect(await readFile(backendTerminated, 'utf8')).toBe('terminated');
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

  it('launches in place without setup and preserves explicit launch arguments', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vs-launcher-test-'));
    const backendTerminated = join(tempDir, 'backend-terminated');
    const backendArgs = join(tempDir, 'backend-args.json');
    const defaultsInvoked = join(tempDir, 'defaults-invoked');
    const backendCwd = join(tempDir, 'backend-cwd');
    const backend = await writeExecutable(
      'in-place-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

if (process.argv.includes('tui-defaults')) {
  writeFileSync(${JSON.stringify(defaultsInvoked)}, JSON.stringify(process.argv.slice(2)));
  console.log(JSON.stringify({theme: 'solarized-light'}));
  process.exit(0);
}
writeFileSync(${JSON.stringify(backendCwd)}, process.cwd());
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
    const frontend = await writeExecutable(
      'in-place-frontend.mjs',
      `
import {writeFileSync} from 'node:fs';
writeFileSync(process.env.VIBESYS_FAKE_FRONTEND_THEME_FILE, process.env.VIBESYS_THEME ?? '');
process.exit(0);
`,
    );

    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;
    process.env['VIBESYS_FAKE_BACKEND_TERM_FILE'] = backendTerminated;
    process.env['VIBESYS_FAKE_BACKEND_ARGS_FILE'] = backendArgs;
    const frontendTheme = join(tempDir, 'frontend-theme');
    process.env['VIBESYS_FAKE_FRONTEND_THEME_FILE'] = frontendTheme;
    process.chdir(tempDir);

    await expect(launch([])).resolves.toBe(0);
    const implicitArgs = JSON.parse(await readFile(backendArgs, 'utf8')) as string[];
    expect(implicitArgs).not.toContain('--runs-dir');
    expect(implicitArgs).not.toContain('--input');
    expect(implicitArgs).not.toContain('--repo');
    expect(implicitArgs).not.toContain('--exp-name');
    // Without --theme the frontend asks over the control channel, so no
    // defaults process runs and the environment carries no theme.
    await expect(access(defaultsInvoked)).rejects.toThrow();
    expect(await realpath(await readFile(backendCwd, 'utf8'))).toBe(await realpath(tempDir));
    expect(await readFile(frontendTheme, 'utf8')).toBe('');

    await expect(
      launch([
        '--input',
        'examples/queue-spsc',
        '--runs-dir',
        '/repo/legacy-runs',
        '--theme',
        'solarized-dark',
      ]),
    ).resolves.toBe(0);
    const explicitArgs = JSON.parse(await readFile(backendArgs, 'utf8')) as string[];
    expect(explicitArgs.filter(argument => argument === '--runs-dir')).toHaveLength(1);
    expect(explicitArgs[explicitArgs.indexOf('--runs-dir') + 1]).toBe('/repo/legacy-runs');
    await expect(access(defaultsInvoked)).rejects.toThrow();
    expect(await readFile(frontendTheme, 'utf8')).toBe('solarized-dark');
    await access(backendTerminated);
  });

  it('rejects an unknown --theme before starting any process', async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vibesys-launcher-'));
    const backend = await writeExecutable('unused-backend.mjs', 'process.exit(0);');
    const frontend = await writeExecutable('unused-frontend.mjs', 'process.exit(0);');
    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    await expect(launch(['--theme', 'monokai'])).resolves.toBe(2);
  });

  it('returns the backend argument error for an explicitly empty runs directory', async () => {
    // The Python side (rejecting an empty --runs-dir and streaming
    // configuration_failed) is covered by tests/entrypoints/test_headless.py
    // and tests/server/test_runtime.py. This test only needs a backend that
    // behaves that way, so it uses a fake one instead of the developer's
    // .venv interpreter.
    tempDir = await mkdtemp(join(tmpdir(), 'vibesys-launcher-'));
    const frontendMarker = join(tempDir, 'frontend-started');
    const failureMarker = join(tempDir, 'configuration-failure.json');
    const backendArgs = join(tempDir, 'backend-args.json');
    const backend = await writeExecutable(
      'failing-backend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createServer} from 'node:net';

writeFileSync(${JSON.stringify(backendArgs)}, JSON.stringify(process.argv.slice(2)));
const socketPath = process.argv[process.argv.indexOf('--control-socket') + 1];
const server = createServer(socket => {
  socket.once('data', data => {
    const request = JSON.parse(data.toString().split('\\n')[0]);
    const event = {
      sequence: 1,
      type: 'configuration_failed',
      data: {
        code: 'invalid_arguments',
        stage: 'argument_parsing',
        message: 'argument --runs-dir: must not be empty',
      },
    };
    socket.write(JSON.stringify({
      protocol_version: 1,
      request_id: request.request_id,
      timestamp: '1970-01-01T00:00:00Z',
      type: 'event',
      event,
    }) + '\\n');
    // Exit once the client has read the failure and hung up.
    socket.once('close', () => process.exit(2));
  });
});
server.listen(socketPath);
`,
    );
    const frontend = await writeExecutable(
      'unused-frontend.mjs',
      `
import {writeFileSync} from 'node:fs';
import {createConnection} from 'node:net';
writeFileSync(${JSON.stringify(frontendMarker)}, 'started');
// The launcher no longer waits for the socket, so the client retries like
// the real backend client does.
const socket = await connectWithRetry(process.env.VIBESYS_CONTROL_SOCKET);
let buffer = '';
socket.write(JSON.stringify({
  protocol_version: 1,
  request_id: 'launcher-empty-runs-dir',
  timestamp: '1970-01-01T00:00:00Z',
  type: 'subscribe',
  after_sequence: 0,
}) + '\\n');
socket.setEncoding('utf8');
socket.on('data', chunk => {
  buffer += chunk;
  while (buffer.includes('\\n')) {
    const newline = buffer.indexOf('\\n');
    const message = JSON.parse(buffer.slice(0, newline));
    buffer = buffer.slice(newline + 1);
    if (message.type === 'event' && message.event.type === 'configuration_failed') {
      writeFileSync(${JSON.stringify(failureMarker)}, JSON.stringify(message.event));
      socket.end();
      return;
    }
  }
});
socket.once('close', () => process.exit(0));

async function connectWithRetry(path) {
  const deadline = Date.now() + 30000;
  while (true) {
    try {
      return await new Promise((resolve, reject) => {
        const socket = createConnection(path);
        socket.once('connect', () => resolve(socket));
        socket.once('error', reject);
      });
    } catch (error) {
      if (Date.now() > deadline) throw error;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  }
}
`,
    );
    process.env['VIBESYS_PYTHON'] = backend;
    process.env['VIBESYS_TUI_RUNTIME'] = process.execPath;
    process.env['VIBESYS_TUI_ENTRYPOINT'] = frontend;

    await expect(
      launch(['--stub-agent', '--local', '--max-rounds', '0', '--runs-dir=']),
    ).resolves.toBe(2);
    await access(frontendMarker);
    // The launcher forwards the explicitly empty value untouched.
    const forwarded = JSON.parse(await readFile(backendArgs, 'utf8')) as string[];
    expect(forwarded).toContain('--runs-dir=');
    const failure = JSON.parse(await readFile(failureMarker, 'utf8')) as {
      data: {code: string; stage: string; message: string};
    };
    expect(failure.data.code).toBe('invalid_arguments');
    expect(failure.data.stage).toBe('argument_parsing');
    expect(failure.data.message).toContain('argument --runs-dir: must not be empty');
  }, 15_000);
});

async function writeExecutable(name: string, source: string): Promise<string> {
  if (!tempDir) throw new Error('tempDir is required');
  const path = join(tempDir, name);
  await writeFile(path, `#!/usr/bin/env node\n${source}`);
  await chmod(path, 0o755);
  return path;
}
