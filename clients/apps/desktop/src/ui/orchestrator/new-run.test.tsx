import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  Harness,
  HarnessModelsSchema,
  ModelCatalogSchema,
  ReceiptSchema,
  Refusal,
} from '@vibesys/orchestrator-proto';
import {act} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {NewRun} from './new-run.js';
import type {StartRunRequest} from './start-draft.js';
import type {StartHost} from './start-run-form.js';

installDom();

const catalog = (allowedModels: string[]) =>
  create(ModelCatalogSchema, {
    allowedModels,
    harnesses: [
      create(HarnessModelsSchema, {
        harness: Harness.CLAUDE_CODE,
        models: ['model-c', 'model-d'],
      }),
      create(HarnessModelsSchema, {harness: Harness.CODEX, models: ['model-a', 'model-b']}),
    ],
  });

const HOSTS: readonly StartHost[] = [
  {key: 'local', label: 'This Mac', models: null},
  {key: 'ssh:gpu-cluster-1', label: 'gpu-cluster-1', models: null},
];

const open = (
  hosts: readonly StartHost[] = HOSTS,
  progress: CommandProgress | undefined = undefined,
) => {
  const started: {host: string; request: StartRunRequest}[] = [];
  const opened: {host: string; runId: string}[] = [];
  const screen = mount(
    <NewRun
      hosts={hosts}
      progress={progress}
      start={(host, request) => started.push({host, request})}
      retry={() => undefined}
      openRun={(host, runId) => opened.push({host, runId})}
    />,
  );
  return {screen, started, opened};
};

/**
 * Types into a text field as a person does: the field focused, the native value setter, then the
 * input and change events and the key release (happy-dom's React takes a change from `keyup`).
 */
function type(field: HTMLInputElement | HTMLTextAreaElement, text: string): void {
  const prototype = Object.getPrototypeOf(field) as object;
  const set = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  if (set === undefined) throw new Error('a text field has a value setter');
  const View = field.ownerDocument.defaultView as unknown as {Event: typeof Event};
  act(() => {
    field.focus();
    set.call(field, text);
    for (const name of ['input', 'change', 'keyup'])
      field.dispatchEvent(new View.Event(name, {bubbles: true}));
  });
}

const repository = (screen: Screen): HTMLInputElement => {
  const field = screen.container.querySelector<HTMLInputElement>(
    'input[placeholder="/path/to/repository"]',
  );
  if (field === null) throw new Error('the form is not showing');
  return field;
};

/** The control a field's label names, found as a person finds it: by the label. */
function fieldOf<T extends HTMLElement>(screen: Screen, label: string): T {
  const named = [...screen.container.querySelectorAll('label')].find(
    each => each.textContent?.trim() === label,
  );
  const control =
    named === undefined ? null : screen.container.querySelector<T>(`#${named.htmlFor}`);
  if (control === null) throw new Error(`no field named ${label}`);
  return control;
}

function choose(select: HTMLSelectElement, value: string): void {
  const View = select.ownerDocument.defaultView as unknown as {Event: typeof Event};
  act(() => {
    select.value = value;
    select.dispatchEvent(new View.Event('change', {bubbles: true}));
  });
}

const optionsOf = (select: HTMLSelectElement): string[] =>
  [...select.options].map(option => option.value);

/** Opens the form and fills the two fields Start needs. */
const ready = (hosts: readonly StartHost[] = HOSTS, progress?: CommandProgress) => {
  const opened = open(hosts, progress);
  opened.screen.press(opened.screen.control('New run'));
  type(repository(opened.screen), '/work/decode-service');
  type(fieldOf(opened.screen, 'Task'), 'decode');
  return opened;
};

describe('the New run entry', () => {
  it('shows no form until it is pressed, and then the Start run form with its host choice', () => {
    const {screen} = open();
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    screen.press(screen.control('New run'));
    expect(repository(screen).value).toBe('');
    expect(screen.controls('Start run')).toHaveLength(1);
    expect(screen.text()).toContain('Host');
    screen.unmount();
  });

  it('keeps what was typed when the form is put away and brought back', () => {
    const {screen} = open();
    screen.press(screen.control('New run'));
    type(repository(screen), '/work/decode-service');
    const dialog = screen.container.querySelector('dialog');
    if (dialog === null) throw new Error('no dialog');
    const View = dialog.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      dialog.dispatchEvent(new View.Event('cancel', {cancelable: true}));
    });
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    screen.press(screen.control('New run'));
    expect(repository(screen).value).toBe('/work/decode-service');
    screen.unmount();
  });
});

describe('the Start run form’s model control', () => {
  it('offers only the models the host allows, the first already chosen, and starts on it', () => {
    const {screen, started} = ready([
      {key: 'local', label: 'This Mac', models: catalog(['model-b', 'model-d', 'model-a'])},
    ]);
    const model = fieldOf<HTMLSelectElement>(screen, 'Model');
    // The default lead tool runs model-d of the allowed ones, and model-b and model-a are the other's.
    expect(optionsOf(model)).toEqual(['model-d']);
    expect(model.value).toBe('model-d');
    screen.press(screen.control('Start run'));
    expect(started.map(each => each.request.lead)).toEqual([
      {harness: Harness.CLAUDE_CODE, model: 'model-d'},
    ]);
    screen.unmount();
  });

  it('follows the lead tool: the options and the model started on change with it', () => {
    const {screen, started} = ready([
      {key: 'local', label: 'This Mac', models: catalog(['model-b', 'model-d', 'model-a'])},
    ]);
    choose(fieldOf<HTMLSelectElement>(screen, 'Lead tool'), 'codex');
    const model = fieldOf<HTMLSelectElement>(screen, 'Model');
    expect(optionsOf(model)).toEqual(['model-b', 'model-a']);
    expect(model.value).toBe('model-b');
    choose(model, 'model-a');
    screen.press(screen.control('Start run'));
    expect(started[0]?.request.lead).toEqual({harness: Harness.CODEX, model: 'model-a'});
    screen.unmount();
  });

  it('offers the tool’s own models and its default on a host that allows all, and starts on none', () => {
    const {screen, started} = ready([{key: 'local', label: 'This Mac', models: catalog([])}]);
    const model = fieldOf<HTMLSelectElement>(screen, 'Model');
    expect(optionsOf(model)).toEqual(['', 'model-c', 'model-d']);
    expect(model.value).toBe('');
    screen.press(screen.control('Start run'));
    expect(started[0]?.request.lead.model).toBe('');
    screen.unmount();
  });

  it('cannot start when the host allows no model for the tool, and says so', () => {
    const {screen, started} = ready([
      {key: 'local', label: 'This Mac', models: catalog(['model-a'])},
    ]);
    const start = screen.control('Start run');
    expect(start.getAttribute('aria-disabled') ?? String(start.hasAttribute('disabled'))).toBe(
      'true',
    );
    screen.press(start);
    expect(started).toEqual([]);
    expect(screen.text()).toContain('No allowed model runs on this tool.');
    screen.unmount();
  });

  it('cannot start when the host’s settings cannot be read', () => {
    const unreadable = create(ModelCatalogSchema, {unreadablePolicy: true});
    const {screen, started} = ready([{key: 'local', label: 'This Mac', models: unreadable}]);
    screen.press(screen.control('Start run'));
    expect(started).toEqual([]);
    expect(screen.text()).toContain('settings cannot be read');
    screen.unmount();
  });
});

describe('the Start run form’s limits', () => {
  it('start with none unless set, and carry the time and the cost when they are', () => {
    const none = ready();
    none.screen.press(none.screen.control('Start run'));
    expect(none.started[0]?.request).toMatchObject({maxTimeSeconds: null, maxCostUsd: null});
    none.screen.unmount();

    const set = ready();
    choose(fieldOf<HTMLSelectElement>(set.screen, 'Time limit'), '120');
    type(fieldOf<HTMLInputElement>(set.screen, 'Cost limit ($)'), '12.5');
    set.screen.press(set.screen.control('Start run'));
    expect(set.started[0]?.request).toMatchObject({maxTimeSeconds: 7200, maxCostUsd: 12.5});
    set.screen.unmount();
  });

  it('refuse a cost that is not an amount, and say so', () => {
    const {screen, started} = ready();
    type(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), 'lots');
    screen.press(screen.control('Start run'));
    expect(started).toEqual([]);
    expect(screen.text()).toContain('cost limit');
    screen.unmount();
  });
});

describe('the Start run form’s refusal', () => {
  it('names the model the host refused and lists what it allows', () => {
    const receipt = create(ReceiptSchema, {
      commandId: 'c1',
      refusal: Refusal.MODEL_NOT_ALLOWED,
      model: 'model-x',
      allowedModels: ['model-a', 'model-b'],
    });
    const {screen} = open(HOSTS, {_tag: 'Refused', receipt} as CommandProgress);
    screen.press(screen.control('New run'));
    const text = screen.text();
    expect(text).toContain(
      'Start refused \u00b7 Model model-x not allowed \u00b7 allowed: model-a, model-b',
    );
    screen.unmount();
  });
});
