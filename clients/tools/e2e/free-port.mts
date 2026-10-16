import {createServer} from 'node:net';

/** The loopback host the e2e suites' servers bind to and the browsers visit. */
export const E2E_HOST = '127.0.0.1';

/** Ask the OS for a port nothing listens on: bind port 0, read the number back, release it. */
function askForFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once('error', reject);
    probe.listen(0, E2E_HOST, () => {
      const address = probe.address();
      probe.close(() => {
        if (address !== null && typeof address === 'object') resolve(address.port);
        else reject(new Error('the OS did not report the probe port'));
      });
    });
  });
}

/**
 * The port this Playwright run serves on, found once per run. Playwright evaluates the config in
 * the runner and again in every worker, so the first evaluation records its choice in `variable`
 * and the workers, which inherit the environment, read it back: all of them agree, and two runs on
 * one machine (parallel agents, CI jobs) never share a port.
 */
export async function runPort(variable: string): Promise<number> {
  const recorded = process.env[variable];
  if (recorded !== undefined && recorded !== '') return Number(recorded);
  const port = await askForFreePort();
  process.env[variable] = String(port);
  return port;
}
