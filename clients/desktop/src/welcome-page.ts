/**
 * The welcome view: recent runs, one host's panel (This Mac by default: its checkout, live runs,
 * and "Start a run"), and "Connect to host" (a searchable list of `~/.ssh/config` aliases). While a
 * run is shown, only the title strip of this page is visible: the host and connection status, and a
 * click on the host brings this page back. Plain DOM; every action is a request to the main process
 * through the welcome preload (`vibesysWelcome`), which owns hosts and connections, and every choice
 * (filtering, keyboard selection) comes from `welcome-model.ts`.
 */

import type {HostKey} from './host-settings.js';
import {filterHosts, type HostChoice, moveSelection} from './welcome-model.js';
import type {
  ChromeState,
  WelcomeHost,
  WelcomeOverview,
  WelcomeRecent,
  WelcomeRecentStatus,
  WelcomeResult,
  WelcomeRun,
} from './welcome-protocol.js';

interface WelcomeBridge {
  readonly platform: string;
  overview(): Promise<WelcomeResult<WelcomeOverview>>;
  recentStatus(host: HostKey): Promise<WelcomeResult<Record<string, WelcomeRecentStatus>>>;
  hosts(): Promise<WelcomeResult<string[]>>;
  host(host: HostKey): Promise<WelcomeResult<WelcomeHost>>;
  setCheckout(host: HostKey, path: string): Promise<WelcomeResult<string>>;
  signIn(host: HostKey): Promise<WelcomeResult<null>>;
  tasks(host: HostKey, project: string): Promise<WelcomeResult<string[]>>;
  attach(host: HostKey, instance: string): Promise<WelcomeResult<null>>;
  start(host: HostKey, project: string, task: string, args: string): Promise<WelcomeResult<null>>;
  resume(host: HostKey, project: string, run: string): Promise<WelcomeResult<null>>;
  showRun(): Promise<WelcomeResult<null>>;
  showWelcome(): Promise<WelcomeResult<null>>;
  retry(): Promise<WelcomeResult<null>>;
  onChrome(listener: (state: ChromeState) => void): void;
}

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
  }
}

function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  text = '',
  className = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (text !== '') node.textContent = text;
  if (className !== '') node.className = className;
  return node;
}

function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`welcome: #${id} is missing`);
  return node as T;
}

function button(text: string, onClick: () => void, className = ''): HTMLButtonElement {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

const bridge = window.vibesysWelcome;
const banner = byId('banner');
const recentPane = byId('recent');
const hostPanel = byId('host-panel');
const hostSheet = byId('host-sheet');
const hostSearch = byId<HTMLInputElement>('host-search');
const hostList = byId('host-list');
const startSheet = byId('start-sheet');
const chip = byId<HTMLButtonElement>('host-chip');
const conn = byId('conn');
const back = byId<HTMLButtonElement>('back');
const retry = byId<HTMLButtonElement>('retry');

/** The host whose panel is shown. */
let current: HostKey = 'local';
let chrome: ChromeState = {mode: 'welcome', attached: null};

function say(message: string, kind: 'info' | 'error' = 'info'): void {
  banner.textContent = message;
  banner.className = kind === 'error' ? 'status error' : 'status';
}

/** Report a failed request; null when it succeeded. */
function failed<T>(
  result: WelcomeResult<T>,
  where: HTMLElement = banner,
): result is Extract<WelcomeResult<T>, {ok: false}> {
  if (result.ok) return false;
  where.textContent = result.error;
  where.className = 'status error';
  return true;
}

// ---- title strip --------------------------------------------------------------------------------

function dotClass(status: string, stuck: boolean): string {
  if (status === 'connected') return 'dot ok';
  return stuck ? 'dot error' : 'dot warn';
}

function renderChrome(state: ChromeState): void {
  chrome = state;
  document.body.classList.toggle('strip', state.mode === 'run');
  const attached = state.attached;
  chip.hidden = attached === null;
  back.hidden = attached === null || state.mode === 'run';
  retry.hidden = attached === null || !attached.stuck;
  if (attached === null) {
    conn.textContent = '';
    return;
  }
  const dot = element('span', '', dotClass(attached.status, attached.stuck));
  chip.replaceChildren(dot, document.createTextNode(attached.hostLabel));
  chip.title =
    state.mode === 'run' ? 'Show hosts and runs' : `Attached to a run on ${attached.hostLabel}`;
  conn.textContent =
    attached.detail === '' ? attached.status : `${attached.status}: ${attached.detail}`;
  conn.className = attached.stuck ? 'error' : 'muted';
}

chip.addEventListener('click', () => {
  if (bridge === undefined) return;
  void (chrome.mode === 'run' ? bridge.showWelcome() : bridge.showRun());
});
back.addEventListener('click', () => void bridge?.showRun());
retry.addEventListener('click', () => void bridge?.retry());

// ---- recent runs ----------------------------------------------------------------------------------

function statusText(status: WelcomeRecentStatus | undefined): {text: string; className: string} {
  if (status === undefined) return {text: 'checking…', className: 'muted'};
  if (status.kind === 'live') return {text: `running (${status.status})`, className: 'ok'};
  if (status.kind === 'ended') return {text: 'ended', className: 'muted'};
  return {text: `unknown: ${status.detail}`, className: 'warn'};
}

/** Show a recent run's refreshed status and the action it allows. */
function fillRecentCell(
  cell: {readonly status: HTMLElement; readonly action: HTMLElement},
  run: WelcomeRecent,
  status: WelcomeRecentStatus | undefined,
): void {
  const {text, className} = statusText(status);
  cell.status.textContent = text;
  cell.status.className = className;
  cell.action.replaceChildren();
  if (status?.kind === 'live') {
    cell.action.append(
      button('Reattach', () => void attach(run.host, status.instanceId), 'primary'),
    );
  } else if (status?.kind === 'ended') {
    cell.action.append(
      button('Resume…', () => void resume(run.host, run.project, run.runId ?? '')),
    );
  } else if (status?.kind === 'unknown') {
    cell.action.append(button('Open host', () => void showHost(run.host)));
  }
}

async function renderRecent(recent: readonly WelcomeRecent[]): Promise<void> {
  if (bridge === undefined) return;
  if (recent.length === 0) return;
  const table = element('table');
  const head = element('tr');
  for (const title of ['Host', 'Project', 'Task', 'Run', 'Status', '']) {
    head.append(element('th', title));
  }
  table.append(head);
  const cells = new Map<string, {status: HTMLElement; action: HTMLElement}>();
  for (const run of recent) {
    const row = element('tr');
    const status = element('td', 'checking…', 'muted');
    const action = element('td');
    row.append(
      element('td', run.hostLabel),
      element('td', run.project, 'path'),
      element('td', run.task ?? '–'),
      element('td', run.runId ?? run.instanceId),
      status,
      action,
    );
    table.append(row);
    cells.set(`${run.host}\n${run.instanceId}`, {status, action});
  }
  recentPane.replaceChildren(table);
  for (const host of new Set(recent.map(run => run.host))) {
    void bridge.recentStatus(host).then(result => {
      for (const run of recent.filter(entry => entry.host === host)) {
        const cell = cells.get(`${run.host}\n${run.instanceId}`);
        if (cell === undefined) continue;
        const status = result.ok
          ? result.value[run.instanceId]
          : ({kind: 'unknown', detail: result.error} as const);
        fillRecentCell(cell, run, status);
      }
    });
  }
}

// ---- host panel ---------------------------------------------------------------------------------

async function showHost(key: HostKey): Promise<void> {
  if (bridge === undefined) return;
  current = key;
  const label = key === 'local' ? 'This Mac' : key.slice('ssh:'.length);
  hostPanel.replaceChildren(element('h2', label), element('p', `Connecting to ${label}…`, 'muted'));
  const result = await bridge.host(key);
  if (current !== key) return;
  if (!result.ok) {
    const error = element('p', result.error, 'error');
    hostPanel.replaceChildren(element('h2', label), error);
    if (result.authNeeded) {
      hostPanel.append(
        button(
          `Sign in to ${label}`,
          async () => {
            const signedIn = await bridge.signIn(key);
            if (!failed(signedIn, error)) await showHost(key);
          },
          'primary',
        ),
      );
    } else {
      hostPanel.append(button('Try again', () => void showHost(key)));
    }
    return;
  }
  renderHost(result.value);
}

function renderHost(host: WelcomeHost): void {
  const title = element('h2', host.label);
  hostPanel.replaceChildren(title);
  if (host.checkout === null) {
    hostPanel.append(checkoutForm(host, host.suggestedCheckout ?? ''));
    return;
  }
  const line = element('div', '', 'row');
  const change = button(
    'Change',
    () => {
      line.replaceWith(checkoutForm(host, host.checkout ?? ''));
    },
    'link',
  );
  line.append(
    element('span', 'VibeSys checkout:', 'muted'),
    element('code', host.checkout),
    change,
  );
  hostPanel.append(line);
  hostPanel.append(runTable(host, host.runs));
  const actions = element('div', '', 'row');
  actions.append(
    button('Start a run…', () => openStart(host), 'primary'),
    button('Refresh', () => void showHost(host.key)),
  );
  hostPanel.append(actions);
}

/** "Where is your VibeSys checkout on HOST?", checked by the host before it is saved. */
function checkoutForm(host: WelcomeHost, value: string): HTMLElement {
  const form = element('form');
  const question = element('p', `Where is your VibeSys checkout on ${host.label}?`);
  const hint = element(
    'p',
    'The directory you cloned VibeSys into (its pyproject.toml declares the vibesys project). ' +
      'The app runs VibeSys there with uv.',
    'muted',
  );
  const row = element('div', '', 'row');
  const input = element('input');
  input.value = value;
  input.placeholder = '/home/me/src/vibesys';
  input.spellcheck = false;
  const save = element('button', 'Check and save', 'primary');
  save.type = 'submit';
  row.append(input, save);
  const status = element('p', '', 'status');
  if (host.suggestedCheckout !== null && value === host.suggestedCheckout) {
    status.textContent = 'Suggested by a VibeSys server running on this host.';
  }
  form.append(question, hint, row, status);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (bridge === undefined) return;
    status.textContent = `Checking ${input.value.trim()} on ${host.label}…`;
    status.className = 'status';
    save.disabled = true;
    const result = await bridge.setCheckout(host.key, input.value);
    save.disabled = false;
    if (failed(result, status)) return;
    await showHost(host.key);
  });
  setTimeout(() => input.focus(), 0);
  return form;
}

function runTable(host: WelcomeHost, runs: readonly WelcomeRun[]): HTMLElement {
  if (runs.length === 0) return element('p', `No live runs on ${host.label}.`, 'muted');
  const table = element('table');
  const head = element('tr');
  for (const title of ['Run', 'Project', 'Status', 'Started', ''])
    head.append(element('th', title));
  table.append(head);
  for (const run of runs) {
    const row = element('tr');
    const started = run.startedAt === 0 ? '' : new Date(run.startedAt * 1000).toLocaleString();
    row.append(
      element('td', run.runId ?? run.id),
      element('td', run.projectRoot, 'path'),
      element('td', run.status),
      element('td', started),
    );
    const cell = element('td');
    if (run.blocked === null)
      cell.append(button('Attach', () => void attach(host.key, run.id), 'primary'));
    else cell.append(element('span', run.blocked, 'warn'));
    row.append(cell);
    table.append(row);
  }
  return table;
}

async function attach(host: HostKey, instance: string): Promise<void> {
  if (bridge === undefined) return;
  say(`Attaching to ${instance}…`);
  const result = await bridge.attach(host, instance);
  if (!failed(result)) say('');
}

async function resume(host: HostKey, project: string, run: string): Promise<void> {
  if (bridge === undefined) return;
  if (
    !window.confirm(`Resume run ${run || '(latest)'} in ${project}? It continues the run's agents.`)
  )
    return;
  say(`Resuming ${run || 'the latest run'}…`);
  const result = await bridge.resume(host, project, run);
  if (!failed(result)) say('');
}

// ---- start a run ----------------------------------------------------------------------------------

const projectInput = byId<HTMLInputElement>('project');
const projectList = byId('projects');
const tasksPane = byId('tasks');
const argsInput = byId<HTMLInputElement>('args');
const startSummary = byId('start-summary');
const startStatus = byId('start-status');
const startGo = byId<HTMLButtonElement>('start-go');
let startHost: WelcomeHost | null = null;
let chosenTask: string | null = null;

function openStart(host: WelcomeHost): void {
  startHost = host;
  chosenTask = null;
  byId('start-title').textContent = `Start a run on ${host.label}`;
  projectList.replaceChildren(
    ...host.projects.map(project => {
      const option = element('option');
      option.value = project;
      return option;
    }),
  );
  projectInput.value = host.projects[0] ?? '';
  tasksPane.replaceChildren();
  argsInput.value = '';
  startStatus.textContent = '';
  summarize();
  startSheet.hidden = false;
  projectInput.focus();
  if (projectInput.value !== '') void listTasks();
}

function summarize(): void {
  const project = projectInput.value.trim();
  startGo.disabled = chosenTask === null || project === '';
  startSummary.textContent =
    chosenTask === null
      ? 'Pick a project, then one of its tasks.'
      : `Runs vibesys --detach --task ${chosenTask}${argsInput.value.trim() === '' ? '' : ` ${argsInput.value.trim()}`} in ${project}. Agent runs cost tokens.`;
}

async function listTasks(): Promise<void> {
  if (bridge === undefined || startHost === null) return;
  const host = startHost;
  const project = projectInput.value;
  chosenTask = null;
  tasksPane.replaceChildren(element('span', 'Reading tasks…', 'muted'));
  summarize();
  const result = await bridge.tasks(host.key, project);
  if (startHost !== host || projectInput.value !== project) return;
  if (!result.ok) {
    tasksPane.replaceChildren(element('span', result.error, 'error'));
    return;
  }
  if (result.value.length === 0) {
    tasksPane.replaceChildren(element('span', 'This project defines no tasks.', 'warn'));
    return;
  }
  tasksPane.replaceChildren(
    ...result.value.map((task, index) => {
      const label = element('label');
      const radio = element('input');
      radio.type = 'radio';
      radio.name = 'task';
      radio.value = task;
      radio.addEventListener('change', () => {
        chosenTask = task;
        summarize();
      });
      if (index === 0 && result.value.length === 1) {
        radio.checked = true;
        chosenTask = task;
      }
      label.append(radio, document.createTextNode(task));
      return label;
    }),
  );
  summarize();
}

byId('list-tasks').addEventListener('click', () => void listTasks());
projectInput.addEventListener('change', () => void listTasks());
projectInput.addEventListener('keydown', event => {
  if (event.key === 'Enter') void listTasks();
});
argsInput.addEventListener('input', summarize);
byId('start-cancel').addEventListener('click', () => {
  startSheet.hidden = true;
});
startGo.addEventListener('click', async () => {
  if (bridge === undefined || startHost === null || chosenTask === null) return;
  startStatus.textContent = `Starting ${chosenTask} on ${startHost.label}…`;
  startStatus.className = 'status';
  startGo.disabled = true;
  const result = await bridge.start(startHost.key, projectInput.value, chosenTask, argsInput.value);
  startGo.disabled = false;
  if (failed(result, startStatus)) return;
  startSheet.hidden = true;
});

// ---- connect to host ------------------------------------------------------------------------------

let aliases: string[] = [];
let choices: HostChoice[] = [];
let selected = -1;

async function openHostSheet(): Promise<void> {
  if (bridge === undefined) return;
  hostSheet.hidden = false;
  hostSearch.value = '';
  hostSearch.focus();
  const result = await bridge.hosts();
  aliases = result.ok ? result.value : [];
  if (!result.ok) say(result.error, 'error');
  renderHostList();
}

function renderHostList(): void {
  choices = filterHosts(aliases, hostSearch.value);
  selected = choices.length === 0 ? -1 : Math.min(Math.max(selected, 0), choices.length - 1);
  hostList.replaceChildren(
    ...choices.map((choice, index) => {
      const item = element('li');
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(index === selected));
      item.append(
        element('span', choice.alias),
        element('span', choice.typed ? 'connect to this destination' : 'ssh config', 'muted'),
      );
      item.addEventListener('click', () => pickHost(choice));
      return item;
    }),
  );
  if (choices.length === 0) {
    hostList.replaceChildren(element('li', 'No matching hosts.', 'muted'));
  }
  hostList.querySelector('[aria-selected="true"]')?.scrollIntoView({block: 'nearest'});
}

function pickHost(choice: HostChoice): void {
  hostSheet.hidden = true;
  void showHost(`ssh:${choice.alias}`);
}

hostSearch.addEventListener('input', () => {
  selected = 0;
  renderHostList();
});
hostSearch.addEventListener('keydown', event => {
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    selected = moveSelection(selected, event.key === 'ArrowDown' ? 1 : -1, choices.length);
    renderHostList();
  } else if (event.key === 'Enter') {
    const choice = choices[selected];
    if (choice !== undefined) pickHost(choice);
  }
});
byId('connect').addEventListener('click', () => void openHostSheet());

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (!hostSheet.hidden) hostSheet.hidden = true;
  else if (!startSheet.hidden) startSheet.hidden = true;
});
for (const sheet of [hostSheet, startSheet]) {
  sheet.addEventListener('click', event => {
    if (event.target === sheet) sheet.hidden = true;
  });
}

// ---- start ----------------------------------------------------------------------------------------

async function load(): Promise<void> {
  if (bridge === undefined) return;
  document.documentElement.dataset['platform'] = bridge.platform;
  bridge.onChrome(renderChrome);
  const overview = await bridge.overview();
  if (failed(overview)) return;
  renderChrome(overview.value.chrome);
  if (overview.value.settingsProblem !== null) say(overview.value.settingsProblem, 'error');
  await renderRecent(overview.value.recent);
  const fromHash = decodeURIComponent(window.location.hash.slice(1));
  await showHost(fromHash === '' ? 'local' : fromHash);
}

window.addEventListener('hashchange', () => {
  const key = decodeURIComponent(window.location.hash.slice(1));
  if (key !== '') void showHost(key);
});

if (bridge === undefined) {
  say('This page runs inside the VibeSys desktop app.', 'error');
} else {
  void load();
}
