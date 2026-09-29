/**
 * Stands in for `vibesys web home` in src/main/home.test.ts. Modes:
 *   serve           announce, answer /health and /pid, exit 0 on SIGINT (as KeyboardInterrupt does)
 *   stubborn        serve, but ignore SIGINT
 *   unhealthy       serve, but answer /health with 503
 *   silent          serve, but never announce
 *   announce <url>  print a running home server's URL and exit 0 (run_home's reuse path)
 *   fail            write a traceback to stderr and exit 3
 */
import {createServer} from 'node:http';
import type {AddressInfo} from 'node:net';

const [mode = 'serve', url = ''] = process.argv.slice(2);

function serve(): void {
  const server = createServer((request, response) => {
    const path = request.url?.split('?')[0];
    if (path === '/health' && mode === 'unhealthy') response.writeHead(503);
    response.end(path === '/pid' ? String(process.pid) : 'vibesys-ok\n');
  });
  server.listen(0, '127.0.0.1', () => {
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

if (mode === 'announce') {
  process.stdout.write(`VibeSys home: ${url}\n`);
} else if (mode === 'fail') {
  process.stderr.write(
    'Traceback (most recent call last):\nOSError: [Errno 48] Address already in use\n',
  );
  process.exitCode = 3;
} else {
  serve();
}
