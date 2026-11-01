/**
 * Settings scoped to a machine: the Machine selector, each machine's Clusters and clouds and Agent
 * logins read from that machine's helper, a cluster saved only once its connection test passes,
 * and Add a machine's errors under their own fields. The fake helper answers for three machines:
 * This Mac (set up), host-b (nothing set up, Claude Code not signed in) and host-c (a VibeSys too
 * old to say). Drawn in a DOM over the world of the browser journeys; these held no layout, so
 * they ran in Chromium for nothing.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {fixtureNamed} from '@vibesys/run-fixtures';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {fillIn, pickLabel, press} from './testing/dom-controls.js';
import {all, byText, maybe, one, see, settle, textOf, until} from './testing/dom-query.js';
import type {FakeBridgeSeed, FakeMachineSettings} from './testing/fake-bridge.js';
import {seedOf} from './testing/fixture-seed.js';
import {SEED} from './testing/seed.js';
import {SETTINGS_MACHINES, type SectionName} from './testing/settings-pages.js';
import {type OpenShell, openShell} from './testing/shell-world.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));
beforeAll(() => {
  for (const name of installed)
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});
afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT'])
    Reflect.deleteProperty(globalThis, name);
  await window.happyDOM.close();
});

const shells: OpenShell[] = [];
afterEach(async () => {
  for (const shell of shells.splice(0)) await shell.close();
});

let clock = new FakeClock();

/** Settings, opened over the world's machines (or `seed`), as the rail opens it. */
async function openSettings(
  seed: FakeBridgeSeed = {...SEED, machines: SETTINGS_MACHINES},
): Promise<OpenShell> {
  clock = new FakeClock();
  const shell = await openShell({kind: 'settings'}, {schedule: clock.schedule, seed});
  shells.push(shell);
  return shell;
}

const main = (shell: OpenShell): HTMLElement => one(shell.container, {role: 'main'});
const nav = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'Settings sections'});

/**
 * A section's button as the wide window lists it, not the disclosure that stands for the list
 * when the window is narrow.
 */
const sectionButton = (shell: OpenShell, name: string | RegExp, exact = false): HTMLElement => {
  const found = all(nav(shell), {role: 'button', name, exact}).filter(
    each => !each.hasAttribute('aria-controls'),
  );
  if (found.length !== 1) throw new Error(`${found.length} sections named ${String(name)}`);
  return found[0] as HTMLElement;
};

/** Opens `section` of the machine `machine` (its label in the selector). */
async function openSection(
  shell: OpenShell,
  section: SectionName,
  machine: string | null = null,
): Promise<void> {
  await press(sectionButton(shell, section, true));
  await settle();
  if (machine !== null) {
    await pickLabel(
      one(nav(shell), {role: 'combobox', name: 'Machine'}) as HTMLSelectElement,
      machine,
    );
    await settle();
  }
  await shell.read();
  await until(
    `the ${section} heading`,
    () => maybe(main(shell), {role: 'heading', level: 1, name: section, exact: true}) !== null,
  );
}

/** What the fake helper was asked to do. */
const done = (): FakeMachineSettings =>
  (globalThis.window as unknown as {fakeHost: {machineSettings: FakeMachineSettings}}).fakeHost
    .machineSettings;

const card = (shell: OpenShell, name: string): HTMLElement => {
  const found = [...main(shell).querySelectorAll<HTMLElement>('.vs-card')].filter(
    each => maybe(each, {role: 'heading', name}) !== null,
  );
  if (found.length !== 1) throw new Error(`${found.length} cards named ${name}`);
  return found[0] as HTMLElement;
};

const button = (scope: Element, name: string): HTMLElement =>
  one(scope, {role: 'button', name, exact: true});
const textbox = (scope: Element, name: string): HTMLInputElement =>
  one(scope, {role: 'textbox', name, exact: true}) as HTMLInputElement;
const closed = (element: Element): boolean => element.getAttribute('aria-disabled') === 'true';

test('the machine selector is at the top, with the machine sections above This app', async () => {
  const shell = await openSettings();
  const items = [
    ...nav(shell).querySelectorAll(
      'label, button:not(.vs-section-toggle), h2, span.vs-sidebar-title',
    ),
  ].map(node => node.textContent?.trim() ?? '');
  expect(items.indexOf('Machine')).toBe(0);
  expect(items.indexOf('Clusters and clouds')).toBeLessThan(items.indexOf('This app'));
  expect(items.indexOf('Agent logins')).toBeLessThan(items.indexOf('This app'));
  for (const name of ['Appearance', 'Machines', 'Notifications']) {
    expect(items.findIndex(item => item.startsWith(name))).toBeGreaterThan(
      items.indexOf('This app'),
    );
  }
  expect(
    all(one(nav(shell), {role: 'combobox', name: 'Machine'}), {role: 'option'}).map(option =>
      textOf(option),
    ),
  ).toEqual(['This Mac', 'host-b', 'host-c']);
});

test('Clusters and clouds lists a set-up machine’s clusters, Modal, SkyPilot and where it is stored', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Clusters and clouds');
  expect(byText(main(shell), 'File', true)[0]?.getAttribute('title')).toBe('Stored on This Mac');
  await see(main(shell), /compute\.toml$/);
  for (const name of ['lab-h100', 'campus', 'cluster-m']) {
    one(main(shell), {role: 'heading', name});
  }
  await see(main(shell), 'From the operator file');
  await see(main(shell), 'SSH · lab-login');
  await see(main(shell), 'Login node', true);
  await see(card(shell, 'Modal'), 'Token found');
  await see(card(shell, 'SkyPilot'), 'Preview');
  // A cluster of the operator file is listed and never removed here.
  expect(closed(button(card(shell, 'cluster-m'), 'Remove'))).toBe(true);
});

test('another machine shows its own settings: empty, no Modal token, or too old to say', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Clusters and clouds', 'host-b');
  await see(main(shell), 'No Slurm cluster yet');
  await see(card(shell, 'Modal'), 'No token');
  expect(all(main(shell), {role: 'heading', name: 'lab-h100'})).toHaveLength(0);
  await pickLabel(
    one(nav(shell), {role: 'combobox', name: 'Machine'}) as HTMLSelectElement,
    'host-c',
  );
  await settle();
  // The detail is the status word's description, not on the page.
  const tooOld = (): Element | undefined => byText(main(shell), 'Needs an update', true)[0];
  await until('Needs an update', () => tooOld() !== undefined);
  expect(tooOld()?.getAttribute('aria-description') ?? '').toMatch(
    /^The VibeSys on host-c is too old for this/,
  );
  expect(textOf(main(shell))).not.toMatch(/is too old for this/);
  await press(sectionButton(shell, 'Agent logins', true));
  await settle();
  await until('Needs an update', () => tooOld() !== undefined);
  expect(tooOld()?.getAttribute('aria-description') ?? '').toMatch(
    /^The VibeSys on host-c is too old for this/,
  );
});

test('a cluster is saved only after Test connection passes, and is then listed and removable', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Clusters and clouds', 'host-b');
  await press(button(main(shell), 'Add a cluster'));
  await settle();
  const form = () => card(shell, 'Add a Slurm cluster');
  const save = () => button(form(), 'Save cluster');
  expect(closed(save())).toBe(true);
  expect(textOf(form())).toContain('Test the connection first.');

  await fillIn(textbox(form(), 'Name'), 'lab-h100');
  await fillIn(textbox(form(), 'Workspace on the cluster'), '/scratch/vibesys');
  await fillIn(textbox(form(), 'SSH host alias'), 'lab-login');
  await settle();
  await press(button(form(), 'Test connection'));
  await see(form(), 'Connection works', true);
  // The test fills the hardware the cluster's scheduler lists.
  expect((one(form(), {role: 'combobox', name: 'GPU vendor'}) as HTMLSelectElement).value).toBe(
    'cuda',
  );
  expect(textbox(form(), 'GPU models').value).toBe('nvidia_h100');
  expect(textbox(form(), 'GPUs per node').value).toBe('8');
  expect(closed(save())).toBe(false);

  // Changing what was tested takes the test back.
  await fillIn(textbox(form(), 'Workspace on the cluster'), '/scratch/other');
  await settle();
  expect(closed(save())).toBe(true);
  expect(byText(form(), 'Test again', true)[0]?.getAttribute('aria-description')).toBe(
    'The settings changed since the last test.',
  );
  await press(button(form(), 'Test connection'));
  await see(form(), 'Connection works', true);

  await press(save());
  await settle();
  await until(
    'lab-h100 listed',
    () => maybe(main(shell), {role: 'heading', name: 'lab-h100'}) !== null,
  );
  expect(maybe(main(shell), {role: 'heading', name: 'Add a Slurm cluster'})).toBeNull();
  expect(done().added).toEqual(['ssh:host-b lab-h100']);

  await press(button(main(shell), 'Remove'));
  await settle();
  await see(main(shell), 'No Slurm cluster yet');
  expect(done().removed).toEqual(['ssh:host-b lab-h100']);
});

for (const [alias, says] of [
  ['unreachable-host', 'Could not reach the cluster'],
  ['no-scheduler', 'The cluster’s scheduler did not answer'],
  ['no-workspace', 'The workspace is not usable'],
] as const) {
  test(`a connection test that fails at ${alias} says where and cannot be saved`, async () => {
    const shell = await openSettings();
    await openSection(shell, 'Clusters and clouds', 'host-b');
    await press(button(main(shell), 'Add a cluster'));
    await settle();
    const form = () => card(shell, 'Add a Slurm cluster');
    await fillIn(textbox(form(), 'Name'), 'lab');
    await fillIn(textbox(form(), 'Workspace on the cluster'), '/w');
    await fillIn(textbox(form(), 'SSH host alias'), alias);
    await settle();
    await press(button(form(), 'Test connection'));
    // The form holds the failure beside the button that retries it, once: no toast repeats it.
    await until(says, () => {
      const alerts = all(form(), {role: 'alert'});
      return alerts.some(each => textOf(each).includes(says));
    });
    const region = maybe(shell.container, {role: 'region', name: 'Notifications'});
    expect(region === null ? [] : all(region, {role: 'alert'})).toHaveLength(0);
    expect(closed(button(form(), 'Save cluster'))).toBe(true);
    expect(done().added).toEqual([]);
  });
}

test('a missing field is named under its own control, and nothing is sent', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Clusters and clouds', 'host-b');
  await press(button(main(shell), 'Add a cluster'));
  await settle();
  const form = () => card(shell, 'Add a Slurm cluster');
  await press(button(form(), 'Test connection'));
  await settle();
  expect(textbox(form(), 'Name').getAttribute('aria-invalid')).toBe('true');
  await see(form(), 'A cluster needs a name.');
  expect(done().tested).toEqual([]);
  await press(button(form(), 'Cancel'));
  await settle();
  expect(maybe(main(shell), {role: 'heading', name: 'Add a Slurm cluster'})).toBeNull();
});

test('Agent logins says each harness is signed in, not signed in, or not installed, and how', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Agent logins');
  const row = (name: string) => card(shell, name);
  await see(row('Claude Code'), 'Signed in');
  await see(row('Claude Code'), 'Account', true);
  await see(row('Codex'), 'API key', true);
  await see(row('Codex'), 'OPENAI_API_KEY', true);
  await see(row('Gemini CLI'), '.gemini/oauth_creds.json', true);
  await see(row('GitHub Copilot'), 'Not signed in');
  await see(row('OpenCode'), 'Cannot tell');
  await see(row('Aider'), 'Not installed', true);
  expect(closed(button(row('Aider'), 'Sign in'))).toBe(true);
  expect(closed(button(row('Gemini CLI'), 'Sign in'))).toBe(true);
  // No secret is ever on the page.
  expect(textOf(main(shell))).not.toMatch(/sk-[A-Za-z0-9]/);
});

test('Sign in starts the harness’s own login on the chosen machine, and Check again reads it anew', async () => {
  const shell = await openSettings();
  await openSection(shell, 'Agent logins', 'host-b');
  const claude = () => card(shell, 'Claude Code');
  await see(claude(), 'Not signed in');
  await press(button(claude(), 'Sign in'));
  await see(claude(), 'Login started', true);
  // What to do next is on the page, as text to read and copy, while the login is pending.
  await see(claude(), 'https://example.com/device', true);
  await see(claude(), 'AB12', true);
  expect(done().logins).toEqual(['ssh:host-b claude']);
  await press(button(claude(), 'Check again'));
  await until(
    'the login to be read anew',
    () => byText(claude(), 'Login started', true).length === 0,
  );
});

test('Add a machine with an unreachable alias says Unreachable, with the raw text on the pointer', async () => {
  const RAW = 'connection down: ssh: connect to host nowhere port 22: Connection refused';
  const shell = await openSettings({
    ...SEED,
    machines: [{host: 'local', label: 'This Mac', checkout: '~/projs/vibesys'}],
    unreachableHosts: ['ssh:nowhere'],
  });
  await press(sectionButton(shell, /Machines/));
  await settle();
  await shell.read();
  await press(button(main(shell), 'Add a machine'));
  await settle();
  await fillIn(textbox(main(shell), 'SSH host'), 'nowhere');
  await fillIn(
    one(main(shell), {role: 'combobox', name: 'VibeSys checkout'}) as HTMLInputElement,
    '~/vibesys',
  );
  await settle();
  await press(button(main(shell), 'Check and add'));
  await until('Unreachable', () => byText(main(shell), 'Unreachable', true).length > 0);
  expect(byText(main(shell), 'Unreachable', true)[0]?.getAttribute('title')).toBe(RAW);
  expect(textOf(main(shell))).not.toMatch(/Connection refused/);
  // The form stays open to be corrected, and nothing was added.
  button(main(shell), 'Check and add');
});

const ADD_CASES = [
  {name: 'nothing typed', host: '', checkout: '', field: 'SSH host', says: /host is required/i},
  {name: 'no host', host: '', checkout: '~/vibesys', field: 'SSH host', says: /host is required/i},
  {name: 'a bad host', host: '-oX=1', checkout: '~/vibesys', field: 'SSH host', says: /usable ssh/},
  {name: 'a known host', host: 'host-b', checkout: '~/vibesys', field: 'SSH host', says: /already/},
  {name: 'no checkout', host: 'host-d', checkout: '', field: 'VibeSys checkout', says: /required/},
  {
    name: 'a bad path',
    host: 'host-d',
    checkout: 'vibesys',
    field: 'VibeSys checkout',
    says: /absolute/,
  },
] as const;

for (const each of ADD_CASES) {
  test(`Add a machine with ${each.name} puts its error under ${each.field}`, async () => {
    const shell = await openSettings(SEED);
    await press(sectionButton(shell, /Machines/));
    await settle();
    await shell.read();
    await press(button(main(shell), 'Add a machine'));
    await settle();
    await fillIn(textbox(main(shell), 'SSH host'), each.host);
    await fillIn(
      one(main(shell), {role: 'combobox', name: 'VibeSys checkout'}) as HTMLInputElement,
      each.checkout,
    );
    await settle();
    await press(button(main(shell), 'Check and add'));
    await settle();
    // Each control's description is the error text below it, and only the named field is invalid.
    const doc = shell.container.ownerDocument;
    const fields = [...shell.container.querySelectorAll('input[aria-label], input[id]')].map(
      input => {
        const label = input.id
          ? [...doc.querySelectorAll('label')].find(each => each.getAttribute('for') === input.id)
          : undefined;
        const errors = (input.getAttribute('aria-describedby') ?? '')
          .split(' ')
          .map(id => doc.getElementById(id))
          .filter(node => node?.getAttribute('data-tone') === 'err')
          .map(node => node?.textContent ?? '');
        return {
          label: label?.textContent ?? '',
          invalid: input.getAttribute('aria-invalid') === 'true',
          errors,
        };
      },
    );
    const named = fields.filter(field => field.invalid);
    expect(named.map(field => field.label)).toEqual([each.field]);
    expect(named[0]?.errors).toHaveLength(1);
    expect(named[0]?.errors[0]).toMatch(each.says);
    expect(named[0]?.errors[0]).not.toContain('""');
  });
}

test('the Add form shows placeholders as examples, never as values', async () => {
  const shell = await openSettings(SEED);
  await press(sectionButton(shell, /Machines/));
  await settle();
  await shell.read();
  await press(button(main(shell), 'Add a machine'));
  await settle();
  const placeholders = [...main(shell).querySelectorAll('input[placeholder]')].map(input =>
    input.getAttribute('placeholder'),
  );
  expect(placeholders).toHaveLength(2);
  for (const placeholder of placeholders) expect(placeholder).toMatch(/^e\.g\. /);
});

test('the Runs dashboard lists the live run of a fixture, once', async () => {
  const shell = await openShell(
    {kind: 'runs'},
    {schedule: new FakeClock().schedule, seed: seedOf(fixtureNamed('weeks-600-ideas'), 'ink')},
  );
  shells.push(shell);
  one(shell.container, {role: 'heading', name: 'Runs'});
  expect(main(shell).querySelectorAll('.vs-overview-tile')).toHaveLength(1);
});
