/** All shared and journey native goldens have one authoritative Python generator. */
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

execFileSync('uv', ['run', 'python', '-m', 'tests.support.journey_fixtures'], {
  cwd: fileURLToPath(new URL('../../../../../', import.meta.url)),
  stdio: 'inherit',
});
