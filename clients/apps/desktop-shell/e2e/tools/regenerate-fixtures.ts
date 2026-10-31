/** Every golden a projector or schema change invalidates has one command: regenerate_goldens.sh. */
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

execFileSync('./scripts/regenerate_goldens.sh', {
  cwd: fileURLToPath(new URL('../../../../../', import.meta.url)),
  stdio: 'inherit',
});
