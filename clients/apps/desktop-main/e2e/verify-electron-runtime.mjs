/** Fail an opt-in E2E run with Electron's native-loader diagnostic before Playwright starts. */
import {spawnSync} from 'node:child_process';
import electronPath from 'electron';

const rootChromiumArgs =
  process.platform === 'linux' && process.getuid?.() === 0 ? ['--no-sandbox'] : [];
const result = spawnSync(electronPath, [...rootChromiumArgs, '--version'], {
  encoding: 'utf8',
  timeout: 15_000,
  windowsHide: true,
});

if (result.error !== undefined || result.status !== 0) {
  const detail = [result.error?.message, result.stderr, result.stdout]
    .filter(value => value !== undefined && value.trim() !== '')
    .map(value => value.trim())
    .join('\n');
  throw new Error(
    `Electron runtime prerequisite check failed before the E2E suite.${
      detail === '' ? '' : `\n${detail}`
    }`,
  );
}
