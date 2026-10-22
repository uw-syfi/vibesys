/**
 * Real-process tier: a host whose VibeSys predates the registry commands, through the whole app.
 * The shipping gateway runs the host's commands, the app's main process classifies the answers,
 * and the app shell shows them. The host's checkout is a directory whose `vibesys` prints what a
 * real older checkout printed for each command (`proto/testdata/older_host/older_host.json`, argparse
 * output recorded offline), so the classifier meets the argparse text of the field, not a Fake.
 */
import {execFileSync} from 'node:child_process';
import {chmod, mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {type ElectronApplication, _electron as electron, expect, test} from '@playwright/test';
import {electronRuntimeArgs} from './electron-launch-policy.mjs';

const repository = fileURLToPath(new URL('../../../../', import.meta.url));
const APP = fileURLToPath(new URL('./older-host-app.mjs', import.meta.url));

interface Corpus {
  readonly rejected: readonly {command: string; exit_code: number; stderr: string}[];
  readonly provided: readonly string[];
}

let buildDirectory: string;
let binary: string;
test.beforeAll(async () => {
  buildDirectory = await mkdtemp('/tmp/vsg-older-build-');
  binary = join(buildDirectory, 'vibesysd');
  execFileSync('go', ['build', '-o', binary, './cmd/vibesysd'], {
    cwd: join(repository, 'gateway'),
    env: {...process.env, CGO_ENABLED: '0'},
    timeout: 30_000,
  });
});
test.afterAll(async () => {
  await rm(buildDirectory, {recursive: true, force: true});
});

/** `vibesys` of the older checkout: each command answers with what the recording says. */
function olderVibesys(corpus: Corpus): string {
  const cases = corpus.rejected.map(
    entry =>
      `  ${entry.command}) cat >&2 <<'RECORDED'\n${entry.stderr.replace(/\n$/, '')}\nRECORDED\n    exit ${entry.exit_code} ;;`,
  );
  const provided = corpus.provided.map(command => `  ${command}) exit 0 ;;`);
  return [
    '#!/bin/sh',
    'if [ "$1" != host ]; then',
    '  echo "usage: vibesys [-h] ..." >&2',
    '  exit 2',
    'fi',
    'case "$2" in',
    ...cases,
    ...provided,
    '  *) echo "usage: vibesys host [-h] ..." >&2; exit 2 ;;',
    'esac',
    '',
  ].join('\n');
}

interface Control {
  logs(): readonly string[];
  loaded(): Promise<void>;
  evaluate(script: string): Promise<unknown>;
}

function control<K extends keyof Control>(
  app: ElectronApplication,
  method: K,
  ...args: Parameters<Control[K]>
): Promise<Awaited<ReturnType<Control[K]>>> {
  return app.evaluate(
    (_electron, request) => {
      const main = (globalThis as typeof globalThis & {vibesysOlderHostE2E?: Control})
        .vibesysOlderHostE2E;
      if (main === undefined) throw new Error('the older host E2E control is unavailable');
      const call = main[request.method] as (...values: unknown[]) => unknown;
      return call.apply(main, request.args) as Awaited<ReturnType<Control[K]>>;
    },
    {method, args},
  );
}

/** Press the button named `label` inside the element matched by `scope`, as a person would. */
const press = (scope: string, label: string): string => `(() => {
  const root = document.querySelector(${JSON.stringify(scope)});
  const button = [...(root?.querySelectorAll('button') ?? [])].find(
    each => (each.getAttribute('aria-label') ?? each.textContent ?? '').trim() === ${JSON.stringify(label)},
  );
  button?.click();
  return button !== undefined;
})()`;

/** What a person can read: the page's text and the labels and tooltips it carries. */
const SCREEN_TEXT = `document.body.innerText + '\\n' + [...document.querySelectorAll('[title], [aria-label]')]
  .map(each => \`\${each.getAttribute('title') ?? ''} \${each.getAttribute('aria-label') ?? ''}\`)
  .join('\\n')`;
/** Anything of the host's own: argparse's words, the command names, or the screen's old verdict. */
const HOST_WORDS =
  /usage:|invalid choice|argument method|list-compute|describe-machine|Unreachable|Reconnect/;

test('a host whose VibeSys predates the registry needs an update on Machines and in Settings', async () => {
  const corpus = JSON.parse(
    await readFile(join(repository, 'proto/testdata/older_host/older_host.json'), 'utf8'),
  ) as Corpus;
  const root = await mkdtemp('/tmp/vsg-older-');
  const checkout = join(root, 'older-checkout');
  await mkdir(join(checkout, '.venv', 'bin'), {recursive: true});
  await mkdir(join(root, 'settings'));
  await writeFile(join(checkout, '.venv', 'bin', 'vibesys'), olderVibesys(corpus));
  await chmod(join(checkout, '.venv', 'bin', 'vibesys'), 0o755);
  const app = await electron.launch({
    args: [APP, ...electronRuntimeArgs()],
    env: {
      ...process.env,
      VIBESYS_GATEWAY_ROOT: root,
      VIBESYS_GATEWAY_BINARY: binary,
      VIBESYS_OLDER_CHECKOUT: checkout,
    },
  });
  try {
    await control(app, 'loaded');
    const screen = () => control(app, 'evaluate', SCREEN_TEXT) as Promise<string>;

    expect(await control(app, 'evaluate', press('nav[aria-label="VibeSys"]', 'Machines'))).toBe(
      true,
    );
    await expect.poll(screen).toContain('Needs an update');
    expect(await screen()).not.toMatch(HOST_WORDS);

    expect(await control(app, 'evaluate', press('body', 'Settings'))).toBe(true);
    expect(
      await control(
        app,
        'evaluate',
        press('nav[aria-label="Settings sections"]', 'Clusters and clouds'),
      ),
    ).toBe(true);
    await expect.poll(screen).toContain('Needs an update');
    expect(await screen()).not.toMatch(HOST_WORDS);

    // What the host wrote is in the main process's log, not on a screen.
    expect(await control(app, 'logs')).toEqual(
      expect.arrayContaining([expect.stringContaining('describe-machine')]),
    );
  } finally {
    try {
      await app.close();
    } finally {
      await rm(root, {recursive: true, force: true});
    }
  }
});
