/**
 * The host and run picker page: pick a host (this Mac or an `~/.ssh/config` alias), see its live
 * detached runs, attach to one, start a new one, or resume a stopped one. Plain DOM; every action is a request to the
 * main process through the picker preload (`vibesysPicker`), which owns hosts and connections.
 */
import type {HostKey, PickerHost, PickerResult, PickerRun} from './picker-protocol.js';

interface PickerBridge {
  hosts(): Promise<PickerResult<PickerHost[]>>;
  runs(host: HostKey): Promise<PickerResult<PickerRun[]>>;
  signIn(host: HostKey): Promise<PickerResult<null>>;
  attach(host: HostKey, instance: string): Promise<PickerResult<null>>;
  start(host: HostKey, project: string, args: string): Promise<PickerResult<null>>;
  resume(host: HostKey, project: string, run: string): Promise<PickerResult<null>>;
  saveSettings(
    host: HostKey,
    vibesysCommand: string,
    pythonCommand: string,
  ): Promise<PickerResult<null>>;
}

declare global {
  interface Window {
    vibesysPicker?: PickerBridge;
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

function byId(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (node === null) throw new Error(`picker: #${id} is missing`);
  return node;
}

const picker = window.vibesysPicker;
const hostList = byId('hosts');
const runPane = byId('runs');
const status = byId('status');
let selected: PickerHost | null = null;

function say(message: string, kind: 'info' | 'error' = 'info'): void {
  status.textContent = message;
  status.className = `status ${kind}`;
}

async function loadHosts(preselect: string | null): Promise<void> {
  if (picker === undefined) return;
  const result = await picker.hosts();
  if (!result.ok) return say(result.error, 'error');
  hostList.replaceChildren();
  for (const host of result.value) {
    const button = element('button', host.label, 'host');
    button.type = 'button';
    button.addEventListener('click', () => void selectHost(host));
    hostList.append(button);
    if (host.key === preselect) void selectHost(host);
  }
}

async function selectHost(host: PickerHost): Promise<void> {
  if (picker === undefined) return;
  selected = host;
  for (const button of hostList.querySelectorAll('button')) {
    button.classList.toggle('selected', button.textContent === host.label);
  }
  runPane.replaceChildren(element('h2', host.label));
  if (host.vibesysCommand !== null) runPane.append(settingsForm(host));
  say(`Listing runs on ${host.label}…`);
  const result = await picker.runs(host.key);
  if (selected !== host) return;
  if (!result.ok) {
    say(result.error, 'error');
    if (result.authNeeded) runPane.append(signInButton(host));
    return;
  }
  say(result.value.length === 0 ? `No live runs on ${host.label}.` : '');
  runPane.append(runTable(host, result.value), startForm(host));
}

function signInButton(host: PickerHost): HTMLElement {
  const button = element('button', `Sign in to ${host.label}`);
  button.type = 'button';
  button.addEventListener('click', async () => {
    if (picker === undefined) return;
    const result = await picker.signIn(host.key);
    if (!result.ok) return say(result.error, 'error');
    await selectHost(host);
  });
  return button;
}

function runTable(host: PickerHost, runs: readonly PickerRun[]): HTMLElement {
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
      element('td', run.projectRoot),
      element('td', run.status),
      element('td', started),
    );
    const cell = element('td');
    if (run.blocked === null) {
      const attach = element('button', 'Attach');
      attach.type = 'button';
      attach.addEventListener('click', async () => {
        if (picker === undefined) return;
        say(`Attaching to ${run.id}…`);
        const result = await picker.attach(host.key, run.id);
        say(result.ok ? '' : result.error, result.ok ? 'info' : 'error');
      });
      cell.append(attach);
    } else {
      cell.append(element('span', run.blocked, 'blocked'));
    }
    row.append(cell);
    table.append(row);
  }
  return table;
}

function startForm(host: PickerHost): HTMLElement {
  const form = element('form', '', 'start');
  const project = element('input');
  project.placeholder = host.vibesysCommand === null ? '/path/to/project' : '~/path/to/project';
  project.required = true;
  const args = element('input');
  args.placeholder = 'run arguments, quoted like a shell (optional)';
  const submit = element('button', 'Start run');
  const run = element('input');
  run.placeholder = 'stopped run id (empty: the latest)';
  const resume = element('button', 'Resume run');
  resume.type = 'button';
  form.append(element('h3', 'Start or resume a detached run'), project, args, submit, run, resume);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (picker === undefined) return;
    say(`Starting a run on ${host.label}…`);
    const result = await picker.start(host.key, project.value, args.value);
    say(result.ok ? '' : result.error, result.ok ? 'info' : 'error');
    if (result.ok) await selectHost(host);
  });
  resume.addEventListener('click', async () => {
    if (picker === undefined || !project.reportValidity()) return;
    say(`Resuming ${run.value.trim() || 'the latest run'} on ${host.label}…`);
    const result = await picker.resume(host.key, project.value, run.value);
    say(result.ok ? '' : result.error, result.ok ? 'info' : 'error');
    if (result.ok) await selectHost(host);
  });
  return form;
}

function settingsForm(host: PickerHost): HTMLElement {
  const form = element('form', '', 'settings');
  const label = element('label', 'vibesys command');
  const input = element('input');
  input.value = host.vibesysCommand ?? '';
  input.spellcheck = false;
  label.append(input);
  const pythonLabel = element('label', 'Python command');
  const python = element('input');
  python.value = host.pythonCommand ?? '';
  python.placeholder = 'derived from the vibesys command';
  python.spellcheck = false;
  pythonLabel.append(python);
  const save = element('button', 'Save');
  form.append(label, pythonLabel, save);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (picker === undefined) return;
    const result = await picker.saveSettings(host.key, input.value, python.value);
    if (!result.ok) return say(result.error, 'error');
    await loadHosts(host.key);
  });
  return form;
}

if (picker === undefined) {
  say('This page runs inside the VibeSys desktop app.', 'error');
} else {
  void loadHosts(new URLSearchParams(window.location.search).get('host'));
}
