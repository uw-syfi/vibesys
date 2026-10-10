/**
 * The welcome view: recent runs, one host's panel (This Mac by default: its checkout, live runs,
 * and "Start a run"), and "Connect to host" (a searchable list of `~/.ssh/config` aliases). While a
 * run is shown, only the title strip of this page is visible: the host and connection status, and a
 * click on the host brings this page back. Plain DOM; every action is a request to the main process
 * through the welcome preload (`vibesysWelcome`), which owns hosts and connections, and every choice
 * (filtering, keyboard selection) comes from `welcome-model.ts`.
 */

import type {HostKey} from './host-settings.js';
import {type StopView, stopKey} from './stop-run.js';
import {WORDMARK} from './welcome-banner.js';
import {
  filterHosts,
  type HostChoice,
  moveSelection,
  NO_TASKS,
  recentChanged,
  stripControls,
  type TaskPicker,
  taskChosen,
  tasksAnswered,
  tasksCleared,
  tasksRequested,
} from './welcome-model.js';
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
  stop(host: HostKey, instance: string, force: boolean): Promise<WelcomeResult<null>>;
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
byId('wordmark').textContent = WORDMARK;
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
const stripStop = byId<HTMLButtonElement>('strip-stop');
const stripForce = byId<HTMLButtonElement>('strip-force');
const stripResume = byId<HTMLButtonElement>('strip-resume');
const stripStopState = byId('strip-stop-state');
/** How often the lists look again while an accepted stop is ending. */
const REFRESH_MS = 3_000;

/** The page behind the sheets, inert while one is open so focus and clicks stay in the sheet. */
const behindSheets = [document.querySelector('header'), document.querySelector('main')];
/** What had focus before the open sheet opened; focus returns there when it closes. */
let sheetOpener: Element | null = null;

function openSheet(sheet: HTMLElement): void {
  if (!sheet.hidden) return;
  sheetOpener = document.activeElement;
  sheet.hidden = false;
  for (const node of behindSheets) if (node !== null) node.inert = true;
}

function closeSheet(sheet: HTMLElement): void {
  if (sheet.hidden) return;
  sheet.hidden = true;
  for (const node of behindSheets) if (node !== null) node.inert = false;
  if (sheetOpener instanceof HTMLElement && sheetOpener.isConnected) sheetOpener.focus();
  sheetOpener = null;
}

/** The host whose panel is shown. */
let current: HostKey = 'local';
let chrome: ChromeState = {mode: 'welcome', attached: null, stops: {}};

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

function dotClass(attached: NonNullable<ChromeState['attached']>): string {
  if (attached.status === 'connected') return 'dot ok';
  if (attached.ended) return 'dot';
  return attached.stuck ? 'dot error' : 'dot warn';
}

function renderChrome(state: ChromeState): void {
  const stopsChanged = JSON.stringify(state.stops) !== JSON.stringify(chrome.stops);
  const runChanged = recentChanged(chrome, state);
  chrome = state;
  document.body.classList.toggle('strip', state.mode === 'run');
  // The strip hides the sheets; an open one would leave the strip inert.
  if (state.mode === 'run') for (const sheet of [hostSheet, startSheet]) closeSheet(sheet);
  const attached = state.attached;
  const controls = stripControls(state);
  chip.hidden = attached === null;
  back.hidden = !controls.back;
  retry.hidden = !controls.retry;
  stripStop.hidden = !controls.stop;
  stripForce.hidden = !controls.force;
  stripResume.hidden = !controls.resume;
  stripStopState.textContent = controls.stopText;
  stripStopState.title = controls.stopText;
  stripStopState.className = controls.stopError ? 'error' : 'muted';
  if (attached === null) {
    conn.textContent = '';
    conn.title = '';
  } else {
    const dot = element('span', '', dotClass(attached));
    chip.replaceChildren(dot, document.createTextNode(attached.hostLabel));
    chip.title =
      state.mode === 'run' ? 'Show hosts and runs' : `Attached to a run on ${attached.hostLabel}`;
    conn.textContent =
      attached.detail === '' ? attached.status : `${attached.status}: ${attached.detail}`;
    conn.title = conn.textContent;
    conn.className = controls.retry ? 'error' : 'muted';
  }
  if (runChanged) void reloadRecent();
  if (stopsChanged) refreshLists();
}

/** Read the recent runs again (the main process just recorded one) and redraw them. */
async function reloadRecent(): Promise<void> {
  if (bridge === undefined) return;
  const overview = await bridge.overview();
  if (overview.ok) await renderRecent(overview.value.recent);
}

chip.addEventListener('click', () => {
  if (bridge === undefined) return;
  void (chrome.mode === 'run' ? bridge.showWelcome() : bridge.showRun());
});
back.addEventListener('click', () => void bridge?.showRun());
retry.addEventListener('click', () => void bridge?.retry());

/** The attached run's instance id and host, when it can be stopped. */
function stripTarget(): {host: HostKey; instance: string} | null {
  const attached = chrome.attached;
  return attached?.instanceId == null ? null : {host: attached.host, instance: attached.instanceId};
}

async function requestStop(host: HostKey, instance: string, force: boolean): Promise<void> {
  if (bridge === undefined) return;
  failed(await bridge.stop(host, instance, force));
}

stripStop.addEventListener('click', () => {
  const target = stripTarget();
  if (target !== null) void requestStop(target.host, target.instance, false);
});
stripForce.addEventListener('click', () => {
  const target = stripTarget();
  if (target !== null) void requestStop(target.host, target.instance, true);
});
stripResume.addEventListener('click', () => {
  const attached = chrome.attached;
  if (attached === null) return;
  void bridge?.showWelcome();
  void resume(attached.host, attached.project, attached.runId ?? '');
});

/** The stop controls of a live run in a list: Stop, "Stopping…", or the failure with Force stop. */
function stopControls(host: HostKey, instance: string): HTMLElement[] {
  const view: StopView | undefined = chrome.stops[stopKey(host, instance)];
  if (view?.phase === 'stopping') return [element('span', view.text, 'muted')];
  const controls: HTMLElement[] = [];
  if (view?.phase === 'error') {
    controls.push(element('span', view.text, 'error'));
    if (view.canForce) {
      controls.push(button('Force stop', () => void requestStop(host, instance, true)));
    }
  }
  controls.push(button('Stop run', () => void requestStop(host, instance, false)));
  return controls;
}

let refreshTimer: ReturnType<typeof setTimeout> | undefined;

/** Redraw the lists from the stop views, polling while a stop that was accepted is ending. */
function refreshLists(): void {
  clearTimeout(refreshTimer);
  void renderRecent(recentRuns);
  void showHost(current, true);
  if (Object.values(chrome.stops).some(view => view.phase === 'stopping')) {
    refreshTimer = setTimeout(refreshLists, REFRESH_MS);
  }
}

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
      ...stopControls(run.host, status.instanceId),
    );
  } else if (status?.kind === 'ended') {
    cell.action.append(
      button('Resume…', () => void resume(run.host, run.project, run.runId ?? '')),
    );
  } else if (status?.kind === 'unknown') {
    cell.action.append(button('Open host', () => void showHost(run.host)));
  }
}

const lastStatus = new Map<string, WelcomeRecentStatus>();
let recentRuns: readonly WelcomeRecent[] = [];

async function renderRecent(recent: readonly WelcomeRecent[]): Promise<void> {
  recentRuns = recent;
  if (bridge === undefined) return;
  if (recent.length === 0) {
    recentPane.replaceChildren(
      element('p', 'No runs yet. Runs you attach to appear here.', 'muted'),
    );
    return;
  }
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
    const known = lastStatus.get(`${run.host}\n${run.instanceId}`);
    if (known !== undefined) fillRecentCell({status, action}, run, known);
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
        if (status !== undefined) lastStatus.set(`${run.host}\n${run.instanceId}`, status);
        fillRecentCell(cell, run, status);
      }
    });
  }
}

// ---- host panel ---------------------------------------------------------------------------------

async function showHost(key: HostKey, quiet = false): Promise<void> {
  if (bridge === undefined) return;
  current = key;
  const label = key === 'local' ? 'This Mac' : key.slice('ssh:'.length);
  if (!quiet) {
    hostPanel.replaceChildren(
      element('h2', label),
      element('p', `Connecting to ${label}…`, 'muted'),
    );
  }
  const result = await bridge.host(key);
  if (current !== key) return;
  if (!result.ok) {
    const error = element('p', result.error, 'error');
    hostPanel.replaceChildren(element('h2', label), error);
    if (result.authNeeded) {
      hostPanel.append(signInButton(key, label, error));
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
      const form = checkoutForm(host, host.checkout ?? '', () => {
        form.replaceWith(line);
        change.focus();
      });
      line.replaceWith(form);
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

/** Sign in to `key` (the host may prompt), then show its panel again; errors go to `where`. */
function signInButton(key: HostKey, label: string, where: HTMLElement): HTMLButtonElement {
  return button(
    `Sign in to ${label}`,
    async () => {
      if (bridge === undefined) return;
      const signedIn = await bridge.signIn(key);
      if (!failed(signedIn, where)) await showHost(key);
    },
    'primary',
  );
}

/**
 * "Where is your VibeSys checkout on HOST?", checked by the host before it is saved. `cancel`, when
 * given (changing a saved checkout), is offered as a Cancel button and Esc.
 */
function checkoutForm(host: WelcomeHost, value: string, cancel?: () => void): HTMLElement {
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
  if (cancel !== undefined) {
    row.append(button('Cancel', cancel));
    form.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      cancel();
    });
  }
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
    if (failed(result, status)) {
      if (result.authNeeded) form.append(signInButton(host.key, host.label, status));
      return;
    }
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
      cell.append(
        button('Attach', () => void attach(host.key, run.id), 'primary'),
        ...stopControls(host.key, run.id),
      );
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
let picker: TaskPicker = NO_TASKS;

function openStart(host: WelcomeHost): void {
  startHost = host;
  picker = tasksCleared(picker);
  byId('start-title').textContent = `Start a run on ${host.label}`;
  projectList.replaceChildren(
    ...host.projects.map(project => {
      const option = element('option');
      option.value = project;
      return option;
    }),
  );
  projectInput.value = host.projects[0] ?? '';
  argsInput.value = '';
  startStatus.textContent = '';
  renderTasks();
  openSheet(startSheet);
  projectInput.focus();
  if (projectInput.value !== '') void listTasks();
}

function summarize(): void {
  const project = projectInput.value.trim();
  const task = picker.chosen;
  startGo.disabled = task === null || project === '';
  startSummary.textContent =
    task === null
      ? 'Pick a project, then one of its tasks.'
      : `Runs vibesys --detach --task ${task}${argsInput.value.trim() === '' ? '' : ` ${argsInput.value.trim()}`} in ${project}. Agent runs cost tokens.`;
}

/** Show the picker's task list; the radio of the chosen task is checked. */
function renderTasks(): void {
  const list = picker.list;
  if (list.kind === 'idle') tasksPane.replaceChildren();
  else if (list.kind === 'loading') {
    tasksPane.replaceChildren(element('span', 'Reading tasks…', 'muted'));
  } else if (list.kind === 'failed') {
    tasksPane.replaceChildren(element('span', list.error, 'error'));
  } else if (list.tasks.length === 0) {
    tasksPane.replaceChildren(element('span', 'This project defines no tasks.', 'warn'));
  } else {
    tasksPane.replaceChildren(
      ...list.tasks.map(task => {
        const label = element('label');
        const radio = element('input');
        radio.type = 'radio';
        radio.name = 'task';
        radio.value = task;
        radio.checked = task === picker.chosen;
        radio.addEventListener('change', () => {
          picker = taskChosen(picker, task);
          summarize();
        });
        label.append(radio, document.createTextNode(task));
        return label;
      }),
    );
  }
  summarize();
}

async function listTasks(): Promise<void> {
  if (bridge === undefined || startHost === null) return;
  const host = startHost;
  picker = tasksRequested(picker);
  const request = picker.request;
  renderTasks();
  const result = await bridge.tasks(host.key, projectInput.value);
  const before = picker;
  picker = tasksAnswered(
    picker,
    request,
    result.ok ? {ok: true, tasks: result.value} : {ok: false, error: result.error},
  );
  if (picker !== before) renderTasks();
}

byId('list-tasks').addEventListener('click', () => void listTasks());
// The listed tasks belong to the project they were read from: editing it drops them.
projectInput.addEventListener('input', () => {
  picker = tasksCleared(picker);
  renderTasks();
});
projectInput.addEventListener('change', () => void listTasks());
projectInput.addEventListener('keydown', event => {
  if (event.key === 'Enter') void listTasks();
});
argsInput.addEventListener('input', summarize);
byId('start-cancel').addEventListener('click', () => closeSheet(startSheet));
startGo.addEventListener('click', async () => {
  const task = picker.chosen;
  if (bridge === undefined || startHost === null || task === null) return;
  startStatus.textContent = `Starting ${task} on ${startHost.label}…`;
  startStatus.className = 'status';
  startGo.disabled = true;
  const result = await bridge.start(startHost.key, projectInput.value, task, argsInput.value);
  startGo.disabled = false;
  if (failed(result, startStatus)) return;
  closeSheet(startSheet);
});

// ---- connect to host ------------------------------------------------------------------------------

let aliases: string[] = [];
let choices: HostChoice[] = [];
let selected = -1;

async function openHostSheet(): Promise<void> {
  if (bridge === undefined) return;
  openSheet(hostSheet);
  hostSearch.value = '';
  selected = 0;
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
  closeSheet(hostSheet);
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
  if (!hostSheet.hidden) closeSheet(hostSheet);
  else if (!startSheet.hidden) closeSheet(startSheet);
});
for (const sheet of [hostSheet, startSheet]) {
  sheet.addEventListener('click', event => {
    if (event.target === sheet) closeSheet(sheet);
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
