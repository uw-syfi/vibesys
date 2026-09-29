/**
 * Stands in for `vibesys web home` in src/main/home.test.ts. Modes:
 *   serve           announce, answer /health and /pid, exit 0 on SIGINT (as KeyboardInterrupt does)
 *   stubborn        serve, but ignore SIGINT
 *   orphaning       serve, exit 0 on SIGINT, but leave a SIGINT-proof group member holding stdout
 *   unhealthy       serve, but answer /health with 503
 *   silent          serve, but never announce
 *   announce <url>  print a running home server's URL and exit 0 (run_home's reuse path)
 *   fail            write a traceback to stderr and exit 3
 *   bind <origin>   attempt the configured port; report the Python bind failure on stderr
 *   denied <origin> report a non-conflict Python bind failure
 */
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import type {AddressInfo} from 'node:net';

const [mode = 'serve', url = ''] = process.argv.slice(2);

function serve(): void {
  const server = createServer((request, response) => {
    const path = request.url?.split('?')[0];
    if (path === '/health' && mode === 'unhealthy') response.writeHead(503);
    response.end(path === '/pid' ? String(process.pid) : 'vibesys-ok\n');
  });
  server.listen(0, '127.0.0.1', async () => {
    if (mode === 'orphaning') await spawnHolder();
    const {port} = server.address() as AddressInfo;
    process.stderr.write(`listening on port ${port}\n`);
    process.stdout.write('a stdout line that is not the announcement: token=fake-token\n');
    if (mode !== 'silent') {
      process.stdout.write(`VibeSys home: http://127.0.0.1:${port}/?token=fake-token\n`);
    }
  });
  process.on('SIGINT', () => {
    if (mode !== 'stubborn') process.exit(0);
  });
}

/** A member of this process group that ignores SIGINT and inherits stdout; resolves once armed. */
function spawnHolder(): Promise<void> {
  const holder = spawn(
    process.execPath,
    ['-e', "process.on('SIGINT', () => {}); console.error('armed'); setInterval(() => {}, 1e6);"],
    {stdio: ['ignore', 'inherit', 'pipe']},
  );
  return new Promise(resolve => holder.stderr.once('data', () => resolve()));
}

if (mode === 'announce') {
  process.stdout.write(`VibeSys home: ${url}\n`);
} else if (mode === 'bind') {
  const server = createServer();
  server.on('error', () => {
    process.stderr.write(`vibesys web home: cannot listen on ${url} (Address already in use).\n`);
    process.exitCode = 1;
  });
  server.listen(Number(new URL(url).port), '127.0.0.1');
} else if (mode === 'denied') {
  process.stderr.write(`vibesys web home: cannot listen on ${url} (Permission denied).\n`);
  process.exitCode = 1;
} else if (mode === 'fail') {
  process.stderr.write(
    'Traceback (most recent call last):\nOSError: [Errno 48] Address already in use\n',
  );
  process.exitCode = 3;
} else {
  serve();
}
