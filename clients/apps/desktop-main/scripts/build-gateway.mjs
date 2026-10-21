/** Build immutable, schema-addressed gateway resources; Go's build cache handles compilation. */
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {mkdir, readFile, readdir, rename, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {API_SCHEMA_HASH as apiHash} from '@vibesys/proto';

const root = fileURLToPath(new URL('../../../../', import.meta.url));
const gateway = join(root, 'gateway');
const buildFlags = ['-trimpath', '-ldflags', '-s -w'];
const output = join(root, 'clients/apps/desktop-main/dist/gateway', apiHash.slice(0, 16));

async function sources(directory) {
  const entries = await readdir(directory, {withFileTypes: true});
  const files = await Promise.all(entries.sort((a, b) => a.name.localeCompare(b.name)).map(async entry => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path);
    if (entry.name.endsWith('.go') || entry.name === 'go.mod' || entry.name === 'go.sum') {
      return [path.slice(gateway.length), await readFile(path)];
    }
    return [];
  }));
  return files.flat();
}
const version = spawnSync('go', ['version'], {encoding: 'utf8'});
if (version.error?.code === 'ENOENT') {
  console.warn('Gateway resources skipped: Go is absent. Run transport v2 must remain off; install Go and rebuild before enabling it.');
} else {
  if (version.status !== 0) throw new Error(version.stderr || 'go version failed');
  const digest = createHash('sha256').update(version.stdout).update(apiHash).update(JSON.stringify(buildFlags));
  for (const source of await sources(gateway)) digest.update(source);
  const fingerprint = digest.digest('hex');
  await mkdir(output, {recursive: true});
  for (const target of ['linux-amd64', 'linux-arm64', 'darwin-arm64']) {
    const binary = join(output, `vibesysd-${target}`);
    const stamp = `${binary}.sha256`;
    let cached = false;
    try {
      const saved = (await readFile(stamp, 'utf8')).split(' ');
      cached = saved[0] === fingerprint && saved[1] === createHash('sha256').update(await readFile(binary)).digest('hex');
    } catch { /* Missing resources are built below. */ }
    if (cached) continue;
    const [GOOS, GOARCH] = target.split('-');
    console.info(`Building gateway ${target} (${apiHash.slice(0, 16)})`);
    const result = spawnSync('go', ['build', ...buildFlags, '-o', `${binary}.tmp`, './cmd/vibesysd'], {
      cwd: gateway, env: {...process.env, CGO_ENABLED: '0', GOOS, GOARCH}, stdio: 'inherit',
    });
    if (result.status !== 0) throw new Error(`gateway build failed: ${target}`);
    await rename(`${binary}.tmp`, binary);
    const sha256 = createHash('sha256').update(await readFile(binary)).digest('hex');
    await writeFile(stamp, `${fingerprint} ${sha256}`);
  }
}
