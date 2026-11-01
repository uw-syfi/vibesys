/**
 * The Usage page, drawn in a DOM over the world of the browser journeys (`testing/usage-world.ts`):
 * what it reads when a host answers, cannot, or does not offer to, the periods it asks for, the
 * tabs, the source and provider cuts, and the price a person sets. These held no layout, so they
 * ran in Chromium for nothing, each at the cost of a page load; here the shell's own stores and
 * screen draw it in a few tens of milliseconds. The browser keeps the screen checks at every
 * width, the geometry, the accessibility check, and the walks that open a run.
 */
import {afterAll, afterEach, beforeAll, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import {Window} from 'happy-dom';
import {fillIn, pickLabel, press} from './testing/dom-controls.js';
import {
  all,
  field,
  maybe,
  notSee,
  one,
  see,
  setChecked,
  settle,
  textOf,
  until,
} from './testing/dom-query.js';
import {
  installFakeUsage,
  installFakeUsageLimits,
  installFakeUsagePrices,
} from './testing/fake-bridge.js';
import {type OpenShell, openShell} from './testing/shell-world.js';
import {seedFor, USAGE_CASES, type UsageCase} from './testing/usage-world.js';

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

interface Asked {
  readonly host: string;
  readonly edges: readonly number[];
}

interface UsageHost {
  fakeHost: {
    usageAsked: Asked[];
    usageRefreshed: unknown[];
    usageSaved: {host: string; baseRevision: number; overrides: {model: string}[]}[];
    usageLimitsAsked: {host: string; maxAgeS: number; now: number}[];
  };
}

const host = (): UsageHost['fakeHost'] => (globalThis.window as unknown as UsageHost).fakeHost;

const caseNamed = (name: string): UsageCase => {
  const found = USAGE_CASES.find(each => each.name === name);
  if (found === undefined) throw new Error(`the cases changed: ${name}`);
  return found;
};

const NOW = Date.UTC(2026, 9, 31, 12, 0, 0);

const INSTALLS = [installFakeUsagePrices, installFakeUsage, installFakeUsageLimits] as const;

/** The app opened on `screen` over the world of `usage`. */
async function openOn(screen: 'runs' | 'usage', usage: UsageCase): Promise<OpenShell> {
  const shell = await openShell(
    {kind: screen},
    {schedule: new FakeClock().schedule, seed: seedFor(usage), installs: INSTALLS, now: NOW},
  );
  shells.push(shell);
  await idle(shell);
  return shell;
}

/** The Usage page, as a person reaches it: opened on it, with what it asks answered. */
const open = (usage: UsageCase): Promise<OpenShell> => openOn('usage', usage);

/** The page has nothing in flight: no loading body and no header word for work in hand. */
async function idle(shell: OpenShell): Promise<void> {
  for (const word of ['Loading', 'Refreshing', 'Scanning', 'Saving']) {
    await until(
      `${word} to go`,
      () =>
        maybe(shell.container, {role: 'status', name: word}) === null && !textOfExact(shell, word),
    );
  }
}

const textOfExact = (shell: OpenShell, word: string): boolean =>
  [...shell.container.querySelectorAll('*')].some(
    each => each.children.length === 0 && (each.textContent ?? '').trim() === word,
  );

const hero = (shell: OpenShell): HTMLElement => {
  const found = shell.container.querySelector<HTMLElement>('#usage-hero');
  if (found === null) throw new Error('the page has no hero figure');
  return found;
};
const heroText = (shell: OpenShell): string => textOf(hero(shell));
const table = (shell: OpenShell, name: string): HTMLElement =>
  one(shell.container, {role: 'table', name});
const rows = (scope: Element): Element[] => all(scope, {role: 'row'});
const button = (scope: Element, name: string, exact = true): HTMLElement =>
  one(scope, {role: 'button', name, exact});
const radio = (scope: Element, name: string): HTMLElement =>
  one(scope, {role: 'radio', name, exact: true});

/** Every text the page holds, one per element that holds text directly. */
function words(shell: OpenShell): string[] {
  return [...shell.container.querySelectorAll('*')].flatMap(element =>
    [...element.childNodes]
      .filter(node => node.nodeType === 3)
      .map(node => node.textContent?.trim() ?? '')
      .filter(text => text !== ''),
  );
}

const rail = (shell: OpenShell): HTMLElement =>
  one(shell.container, {role: 'navigation', name: 'VibeSys'});

async function showScreen(shell: OpenShell, destination: string): Promise<void> {
  await press(one(rail(shell), {role: 'button', name: destination, exact: destination === 'New'}));
  await shell.read();
}

/** A tab or choice of the page's switches. */
async function choose(shell: OpenShell, name: string): Promise<void> {
  await setChecked(radio(shell.container, name), true);
}

async function selectLabel(shell: OpenShell, label: string, option: string): Promise<void> {
  await pickLabel(field(shell.container, label, true) as HTMLSelectElement, option);
  await settle();
}

test('no case of the page writes a sentence or an em dash', async () => {
  for (const usage of USAGE_CASES) {
    const shell = await open(usage);
    const written = words(shell);
    expect(written.length, usage.name).toBeGreaterThan(0);
    for (const text of written) {
      expect(text, `${usage.name}: ${text}`).not.toContain('—');
      expect(text, `${usage.name}: ${text}`).not.toMatch(/[.!?](\s|$)/);
    }
    await shell.close();
    shells.pop();
  }
});

test('a host that predates the question reads Update required and shows no figures', async () => {
  const shell = await open(caseNamed('unsupported-host'));
  await see(shell.container, 'Update required', true);
  expect(all(shell.container, {role: 'table'})).toHaveLength(0);
});

test('a failed question reads Unavailable and Retry asks again', async () => {
  const shell = await open(caseNamed('failed-question'));
  // The body says it once; the header has no word for a failure.
  const unavailable = () =>
    [...shell.container.querySelectorAll('*')].filter(
      each => each.children.length === 0 && (each.textContent ?? '').trim() === 'Unavailable',
    );
  expect(unavailable()).toHaveLength(1);
  const before = host().usageAsked.length;
  await press(button(shell.container, 'Retry'));
  await until('the question asked again', () => host().usageAsked.length > before);
  await idle(shell);
  expect(unavailable()).toHaveLength(1);
});

test('an empty period says No usage, shows no table and no figure of zero', async () => {
  const shell = await open(caseNamed('empty-period'));
  await see(shell.container, 'No usage', true);
  expect(all(shell.container, {role: 'table', name: 'Models'})).toHaveLength(0);
  // The empty state says so once: no figure of zero and no count of sessions above it.
  expect(shell.container.querySelector('#usage-hero')).toBeNull();
  // With no provider there is nothing to switch on or off: no bare header row.
  expect(all(shell.container, {role: 'table', name: 'Providers'})).toHaveLength(0);
});

test('the figures are the hero, the providers, the models, the hours and days, the runs and the agents', async () => {
  const shell = await open(caseNamed('many-runs'));
  // Seven days: the base cost of every series, each bucket ramped 1, 3, 5, 2, 4, 1, 3.
  expect(heroText(shell)).toBe('$817.00');
  expect(rows(table(shell, 'Providers'))).toHaveLength(3);
  expect(rows(table(shell, 'Models'))).toHaveLength(4);
  expect(rows(table(shell, 'Time'))).toHaveLength(8);
  const runs = table(shell, 'Cost by run');
  expect(rows(runs)).toHaveLength(41);
  // The costliest first, each with its cost.
  expect(textOf(rows(runs)[1] as Element)).toContain('$900.00');
  expect(rows(table(shell, 'Cost by agent'))).toHaveLength(7);
  await see(shell.container, 'Per new best', true);
  await see(shell.container, 'Per hour now', true);
});

test('every period asks the host in the buckets it names, in both directions', async () => {
  const shell = await open(caseNamed('one-run'));
  // The page opens on the past week: seven days are eight edges.
  expect(host().usageAsked.at(-1)?.edges).toHaveLength(8);
  const order = [
    ['Past 24 hours', 25],
    ['Past 7 days', 8],
    ['Past 30 days', 31],
    ['Past 90 days', 91],
    ['Past 30 days', 31],
    ['Past 7 days', 8],
    ['Past 24 hours', 25],
  ] as const;
  for (const [label, edges] of order) {
    const before = host().usageAsked.length;
    await selectLabel(shell, 'Period', label);
    await until(`the question for ${label}`, () => host().usageAsked.length > before);
    const last = host().usageAsked.at(-1);
    expect(last?.edges, label).toHaveLength(edges);
    // Whole quarter hours, increasing: what a host accepts.
    for (const [at, edge] of (last?.edges ?? []).entries()) {
      expect(edge % 900_000 === 0, `${label}: ${edge}`).toBe(true);
      if (at > 0) expect(edge).toBeGreaterThan(last?.edges[at - 1] ?? 0);
    }
    await idle(shell);
    expect(rows(table(shell, 'Time')), label).toHaveLength(edges);
  }
});

test('Cost and Tokens switch the figures both ways without asking the host again', async () => {
  const shell = await open(caseNamed('one-run'));
  const before = host().usageAsked.length;
  expect(heroText(shell)).toBe('$817.00');
  await choose(shell, 'Tokens');
  expect(heroText(shell)).toBe('125.4M');
  await choose(shell, 'Cost');
  expect(heroText(shell)).toBe('$817.00');
  expect(host().usageAsked.length).toBe(before);
});

test('the source select and a provider toggle cut the figures by exactly the rows they leave out', async () => {
  const shell = await open(caseNamed('outside-runs'));
  const dollars = (): number => Number(heroText(shell).replace(/[$,]/g, ''));
  const everything = dollars();
  await selectLabel(shell, 'Source', 'VibeSys runs');
  const runsOnly = dollars();
  await selectLabel(shell, 'Source', 'Outside runs');
  const outside = dollars();
  expect(runsOnly + outside).toBeCloseTo(everything, 2);
  expect(outside).toBeGreaterThan(0);
  await selectLabel(shell, 'Source', 'All sources');
  expect(dollars()).toBeCloseTo(everything, 2);
  // A provider turned off takes exactly its rows out, and back on puts them back.
  const codex = () => one(shell.container, {role: 'checkbox', name: 'Codex'});
  await setChecked(codex(), false);
  expect(dollars()).toBeCloseTo(everything - 5 * 19, 2);
  await setChecked(codex(), true);
  expect(dollars()).toBeCloseTo(everything, 2);
});

test('what the host could not price is a chip, and the rates it used are named', async () => {
  const shell = await open(caseNamed('unpriced-models'));
  await see(shell.container, /% unpriced$/);
  // The price list is dated, so a person sees how old the prices are, not only that they are bundled.
  await see(shell.container, 'Prices of 2026-10-30', true);
});

test('a model opens its detail, and its price is set, saved and read back', async () => {
  const shell = await open(caseNamed('one-run'));
  const models = table(shell, 'Models');
  await press(rows(models)[1] as Element);
  await settle();
  const dialog = () => one(shell.container, {role: 'dialog'});
  await press(button(dialog(), 'Close'));
  await settle();
  expect(all(shell.container, {role: 'dialog'})).toHaveLength(0);

  // The Price button sets the price and does not also open the detail.
  await press(all(models, {role: 'button', name: 'Price', exact: true})[0] as Element);
  await settle();
  await setChecked(one(dialog(), {role: 'checkbox', name: 'Automatic', exact: true}), false);
  await fillIn(field(dialog(), 'Input per 1M tokens') as HTMLInputElement, '7');
  await settle();
  await press(button(dialog(), 'Save'));
  await settle();
  await see(dialog(), 'Saved', true);
  const saved = host().usageSaved;
  expect(saved).toHaveLength(1);
  expect(saved[0]?.baseRevision).toBe(0);
  expect(saved[0]?.overrides).toHaveLength(1);
  // The figures are read again, and the row says the price is set.
  await press(button(dialog(), 'Close'));
  await settle();
  await see(models, 'Set', true);
});

test('a price that cannot be saved says which field, and Save waits', async () => {
  const shell = await open(caseNamed('one-run'));
  await press(
    all(table(shell, 'Models'), {role: 'button', name: 'Price', exact: true})[0] as Element,
  );
  await settle();
  const dialog = () => one(shell.container, {role: 'dialog'});
  await setChecked(one(dialog(), {role: 'checkbox', name: 'Automatic', exact: true}), false);
  await fillIn(field(dialog(), 'Output per 1M tokens') as HTMLInputElement, '-1');
  await settle();
  await see(dialog(), 'Invalid', true);
  expect(button(dialog(), 'Save').getAttribute('aria-disabled')).toBe('true');
  expect(host().usageSaved).toEqual([]);
});

test('a run row is titled as the run is everywhere, by its project, and never by its id', async () => {
  const shell = await open(caseNamed('many-runs'));
  const first = rows(table(shell, 'Cost by run'))[1] as Element;
  expect(textOf(first)).toContain('queue-rs');
  expect(textOf(first)).not.toMatch(/2026101\d-\d{6}/);
});

test('Refresh asks the host again', async () => {
  const shell = await open(caseNamed('one-run'));
  const before = host().usageAsked.length;
  await press(button(shell.container, 'Refresh'));
  await until('the question asked again', () => host().usageAsked.length > before);
  await idle(shell);
});

test('Usage asks nothing until it is shown, and again each time it is shown', async () => {
  const shell = await openOn('runs', caseNamed('one-run'));
  one(shell.container, {role: 'heading', name: 'Runs', level: 1});
  expect(host().usageAsked.length).toBe(0);
  await showScreen(shell, 'Usage');
  await until('the figures', () => shell.container.querySelector('#usage-hero') !== null);
  expect(heroText(shell)).toBe('$817.00');
  const first = host().usageAsked.length;
  expect(first).toBeGreaterThan(0);
  await showScreen(shell, 'Runs');
  one(shell.container, {role: 'heading', name: 'Runs', level: 1});
  await showScreen(shell, 'Usage');
  await until('the figures', () => shell.container.querySelector('#usage-hero') !== null);
  expect(heroText(shell)).toBe('$817.00');
  expect(host().usageAsked.length).toBeGreaterThan(first);
});

test('a model no price covers reads Unpriced, and neither it nor its provider is drawn at zero', async () => {
  const shell = await open(caseNamed('unpriced-models'));
  const models = table(shell, 'Models');
  const row = rows(models).find(each => textOf(each).includes('Gpt Next Mini'));
  expect(row, 'the Gpt Next Mini row').toBeDefined();
  expect(textOf(row as Element)).toContain('Unpriced');
  expect(textOf(row as Element)).not.toContain('$0.00');
  const providers = table(shell, 'Providers');
  const codex = rows(providers).find(each => textOf(each).includes('Codex'));
  expect(textOf(codex as Element)).toContain('Unpriced');
  // One layer is drawn: the provider with prices.
  expect(shell.container.querySelectorAll('.vs-area-chart-layer')).toHaveLength(1);
  // Every name is the catalogue's or the id in words, never the id.
  notSee(shell.container, 'gpt-next-mini');
});

test('the tokens tab carries no dollars', async () => {
  const shell = await open(caseNamed('one-run'));
  await choose(shell, 'Tokens');
  for (const name of ['Cache savings', 'Per new best', 'Per hour now']) {
    notSee(shell.container, name, true);
  }
  await choose(shell, 'Cost');
  await see(shell.container, 'Per new best', true);
});

test('the Limits tab shows a card per provider with a bar per window, asks each machine once, and writes no sentence', async () => {
  const shell = await open(caseNamed('one-run'));
  expect(host().usageLimitsAsked).toEqual([]);
  await choose(shell, 'Limits');
  const heading = (name: string) =>
    maybe(shell.container, {role: 'heading', name, level: 2, exact: true});
  await until('the Claude card', () => heading('Claude') !== null);
  expect(heading('Codex')).not.toBeNull();
  // Claude: a session, a weekly and a per-model window; Codex: one weekly window and a reset credit.
  for (const reading of ['61% left', '72% left', '100% left', '97% left']) {
    await see(shell.container, reading, true);
  }
  await see(shell.container, 'Resets in 3 h', true);
  await see(shell.container, '1 resets', true);
  // The period and the source mean nothing here, and the header says no word for work.
  expect(all(shell.container, {role: 'combobox', name: 'Period'})).toHaveLength(0);
  expect(host().usageLimitsAsked).toHaveLength(1);
  // Back to the figures and to the limits again: inside the floor the machine is not asked again.
  await choose(shell, 'Cost');
  expect(shell.container.querySelector('#usage-hero')).not.toBeNull();
  await choose(shell, 'Limits');
  expect(heading('Claude')).not.toBeNull();
  expect(host().usageLimitsAsked).toHaveLength(1);
  for (const text of words(shell)) {
    expect(text, text).not.toContain('—');
    expect(text, text).not.toMatch(/[.!?](\s|$)/);
  }
  // Refresh asks the CLIs at once, whatever the floor.
  await press(button(shell.container, 'Refresh'));
  await until('the limits asked again', () => host().usageLimitsAsked.length === 2);
  expect(host().usageLimitsAsked[1]?.maxAgeS).toBe(0);
});

test('a host that predates the limits question reads Update required on the Limits tab', async () => {
  const shell = await open(caseNamed('unsupported-host'));
  await choose(shell, 'Limits');
  await see(shell.container, 'Update required', true);
  expect(
    maybe(shell.container, {role: 'heading', name: 'Claude', level: 2, exact: true}),
  ).toBeNull();
});
