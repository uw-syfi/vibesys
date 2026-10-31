/** Every golden a projector or schema change invalidates has one command: regenerate_goldens.sh. */
import {execFileSync} from 'node:child_process';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const repository = fileURLToPath(new URL('../../../../../', import.meta.url));

execFileSync(join(repository, 'scripts', 'regenerate_goldens.sh'), {
  cwd: repository,
  stdio: 'inherit',
});
