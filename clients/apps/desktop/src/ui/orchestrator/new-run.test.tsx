import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  Harness,
  HarnessModelsSchema,
  ModelCatalogSchema,
  ReceiptSchema,
  Refusal,
} from '@vibesys/orchestrator-proto';
import {act, useState} from 'react';
import {
  type CommandProgress,
  type FolderListing,
  listingKey,
  NOT_PROVISIONING,
  NOT_READ,
  type RepositoryAnswer,
  type RepositoryChecked,
} from '../../state/index.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {NewRun} from './new-run.js';
import type {StartRunRequest} from './start-draft.js';
import type {StartHost} from './start-run-form.js';

installDom();

/** What a host has said of its catalogue and places, and the commands that ask for them: nothing, for the repository half. */
const NO_NEW_RUN = {catalogue: NOT_READ, evalPlaces: NOT_READ, provision: NOT_PROVISIONING};
const NO_NEW_RUN_COMMANDS = {
  loadNewRun: () => undefined,
  startFromExample: () => undefined,
  forgetProvision: () => undefined,
  beginSetup: () => undefined,
};

/** Opens the dialog and chooses the person's own repository, the half this file is about. */
function openRepository(screen: Screen): void {
  screen.press(screen.control('New run'));
  screen.press(segment(screen, 'My repository'));
}

/** The radio a segment's label names: a segmented control is native radios, which the harness does not list as controls. */
function segment(screen: Screen, label: string): HTMLInputElement {
  const named = [...screen.container.querySelectorAll('label')].find(
    each => each.textContent?.trim() === label,
  );
  const radio = named?.querySelector('input');
  if (radio === null || radio === undefined) throw new Error(`no segment named ${label}`);
  return radio;
}

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

const PASSED: RepositoryChecked = {
  repository: '/work/decode-service',
  answer: {_tag: 'Passed', tasks: []},
};

const HOSTS: readonly StartHost[] = [
  {
    key: 'local',
    label: 'This Mac',
    models: null,
    repositoryChecked: undefined,
    directories: new Map(),
    ...NO_NEW_RUN,
  },
  {
    key: 'ssh:gpu-cluster-1',
    label: 'gpu-cluster-1',
    models: null,
    repositoryChecked: undefined,
    directories: new Map(),
    ...NO_NEW_RUN,
  },
];

const open = (
  hosts: readonly StartHost[] = HOSTS,
  progress: CommandProgress | undefined = undefined,
) => {
  const started: {host: string; request: StartRunRequest}[] = [];
  const opened: {host: string; runId: string}[] = [];
  const begun: {host: string; request: StartRunRequest}[] = [];
  const screen = mount(
    <NewRun
      {...NO_NEW_RUN_COMMANDS}
      beginSetup={(host, request) => begun.push({host, request})}
      hosts={hosts}
      progress={progress}
      start={(host, request) => started.push({host, request})}
      checkRepository={() => undefined}
      listDirectory={() => undefined}
      retry={() => undefined}
      openRun={(host, runId) => opened.push({host, runId})}
    />,
  );
  return {screen, started, opened, begun};
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

/** The text a control is described by that has the tone `tone`, or null when there is none. */
function describedAs(control: HTMLElement, tone: string | null): string | null {
  const ids = (control.getAttribute('aria-describedby') ?? '').split(' ').filter(Boolean);
  const said = ids
    .map(id => control.ownerDocument.getElementById(id))
    .find(each => each !== null && each.getAttribute('data-tone') === tone);
  return said?.textContent ?? null;
}

/** What the form says is wrong under a control's field, or null when it says nothing. */
const errorUnder = (control: HTMLElement): string | null => describedAs(control, 'err');

/** Why Start cannot be pressed, as it says under the button. */
const startReason = (screen: Screen): string | null =>
  describedAs(screen.control('Start run'), null);

const optionsOf = (select: HTMLSelectElement): string[] =>
  [...select.options].map(option => option.value);

/** Opens the form and fills the two fields Start needs. */
const ready = (hosts: readonly StartHost[] = HOSTS, progress?: CommandProgress) => {
  // The host has said the path is a work tree, as a host does for the path typed below.
  const opened = open(
    hosts.map(host => ({...host, repositoryChecked: PASSED})),
    progress,
  );
  openRepository(opened.screen);
  type(repository(opened.screen), '/work/decode-service');
  type(fieldOf(opened.screen, 'Task'), 'decode');
  return opened;
};

describe('the New run entry', () => {
  it('shows no form until it is pressed, and then the Start run form with its host choice', () => {
    const {screen} = open();
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    openRepository(screen);
    expect(repository(screen).value).toBe('');
    expect(screen.controls('Start run')).toHaveLength(1);
    expect(screen.text()).toContain('Host');
    screen.unmount();
  });

  it('keeps what was typed when the form is put away and brought back', () => {
    const {screen} = open();
    openRepository(screen);
    type(repository(screen), '/work/decode-service');
    const dialog = screen.container.querySelector('dialog');
    if (dialog === null) throw new Error('no dialog');
    const View = dialog.ownerDocument.defaultView as unknown as {Event: typeof Event};
    act(() => {
      dialog.dispatchEvent(new View.Event('cancel', {cancelable: true}));
    });
    expect(screen.container.querySelector('input[placeholder="/path/to/repository"]')).toBeNull();
    openRepository(screen);
    expect(repository(screen).value).toBe('/work/decode-service');
    screen.unmount();
  });
});

describe('Set up a task, for a repository that has none yet', () => {
  it('is closed until a repository is named, and then begins a setup in it with no task named', () => {
    const {screen, begun} = open(HOSTS.map(host => ({...host, repositoryChecked: PASSED})));
    openRepository(screen);
    expect(screen.control('Set up a task').getAttribute('aria-disabled')).toBe('true');
    type(repository(screen), '/work/decode-service');
    screen.press(screen.control('Set up a task'));
    expect(begun.map(each => [each.host, each.request.repository])).toEqual([
      ['local', '/work/decode-service'],
    ]);
    screen.unmount();
  });

  it('is not offered for an example, which has its task', () => {
    const {screen} = open();
    screen.press(screen.control('New run'));
    expect(screen.controls('Set up a task')).toHaveLength(0);
    screen.unmount();
  });
});

describe('the Start run form’s model control', () => {
  it('offers only the models the host allows, the first already chosen, and starts on it', () => {
    const {screen, started} = ready([
      {
        key: 'local',
        label: 'This Mac',
        models: catalog(['model-b', 'model-d', 'model-a']),
        repositoryChecked: undefined,
        directories: new Map(),
        ...NO_NEW_RUN,
      },
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
      {
        key: 'local',
        label: 'This Mac',
        models: catalog(['model-b', 'model-d', 'model-a']),
        repositoryChecked: undefined,
        directories: new Map(),
        ...NO_NEW_RUN,
      },
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
    const {screen, started} = ready([
      {
        key: 'local',
        label: 'This Mac',
        models: catalog([]),
        repositoryChecked: undefined,
        directories: new Map(),
        ...NO_NEW_RUN,
      },
    ]);
    const model = fieldOf<HTMLSelectElement>(screen, 'Model');
    expect(optionsOf(model)).toEqual(['', 'model-c', 'model-d']);
    expect(model.value).toBe('');
    screen.press(screen.control('Start run'));
    expect(started[0]?.request.lead.model).toBe('');
    screen.unmount();
  });

  it('cannot start when the host allows no model for the tool, and says so', () => {
    const {screen, started} = ready([
      {
        key: 'local',
        label: 'This Mac',
        models: catalog(['model-a']),
        repositoryChecked: undefined,
        directories: new Map(),
        ...NO_NEW_RUN,
      },
    ]);
    const start = screen.control('Start run');
    expect(start.getAttribute('aria-disabled') ?? String(start.hasAttribute('disabled'))).toBe(
      'true',
    );
    screen.press(start);
    expect(started).toEqual([]);
    const model = fieldOf<HTMLSelectElement>(screen, 'Model');
    expect(model.getAttribute('aria-invalid')).toBe('true');
    expect(errorUnder(model)).toBe('No allowed model runs on this tool.');
    expect(startReason(screen)).toBe('Check the model.');
    screen.unmount();
  });

  it('cannot start when the host’s settings cannot be read', () => {
    const unreadable = create(ModelCatalogSchema, {unreadablePolicy: true});
    const {screen, started} = ready([
      {
        key: 'local',
        label: 'This Mac',
        models: unreadable,
        repositoryChecked: undefined,
        directories: new Map(),
        ...NO_NEW_RUN,
      },
    ]);
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

  it('refuse a cost written in another notation, and say so under the cost field', () => {
    for (const notation of ['0x10', '1e3', '0b11', '0o7']) {
      const {screen, started} = ready();
      const cost = fieldOf<HTMLInputElement>(screen, 'Cost limit ($)');
      type(cost, notation);
      screen.press(screen.control('Start run'));
      expect(started).toEqual([]);
      expect(cost.getAttribute('aria-invalid')).toBe('true');
      expect(errorUnder(cost)).toBe('The cost limit is a number of dollars, such as 25 or 12.50.');
      expect(startReason(screen)).toBe('Check the cost limit.');
      screen.unmount();
    }
  });

  it('read a cost written with the dollar sign the label shows', () => {
    const {screen, started} = ready();
    type(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '$25');
    screen.press(screen.control('Start run'));
    expect(started[0]?.request.maxCostUsd).toBe(25);
    screen.unmount();
  });
});

describe('the Start run form’s required fields', () => {
  it('say under Start what is missing, and under the field once it was typed in and emptied', () => {
    const {screen} = open();
    openRepository(screen);
    const repo = repository(screen);
    expect(repo.getAttribute('aria-invalid')).toBeNull();
    expect(startReason(screen)).toBe('Name the repository to optimize.');

    type(repo, '/work/decode-service');
    type(repo, '');
    expect(repo.getAttribute('aria-invalid')).toBe('true');
    expect(errorUnder(repo)).toBe('Name the repository to optimize.');
    expect(startReason(screen)).toBe('Check the repository.');

    type(repo, '/work/decode-service');
    expect(errorUnder(repo)).toBeNull();
    expect(startReason(screen)).toBe('Name the task to run.');
    const task = fieldOf<HTMLInputElement>(screen, 'Task');
    type(task, '   ');
    expect(errorUnder(task)).toBe('Name the task to run.');
    expect(startReason(screen)).toBe('Check the task.');
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
    openRepository(screen);
    const text = screen.text();
    expect(text).toContain(
      'Start refused \u00b7 Model model-x not allowed \u00b7 allowed: model-a, model-b',
    );
    screen.unmount();
  });
});

/**
 * The form over a host that answers about repository paths the way the connected app does: a
 * question about a path is answered when `answers` has the reply for it, and the form is handed the
 * latest answer with the path it was about. A path `answers` does not know stays unanswered.
 */
const withHostAnswers = (answers: Readonly<Record<string, RepositoryAnswer>>) => {
  const asked: {host: string; repository: string}[] = [];
  const started: StartRunRequest[] = [];
  function Connected(): ReturnType<typeof NewRun> {
    const [checked, setChecked] = useState<RepositoryChecked | undefined>(undefined);
    return (
      <NewRun
        {...NO_NEW_RUN_COMMANDS}
        hosts={HOSTS.map(host => ({...host, repositoryChecked: checked}))}
        listDirectory={() => undefined}
        progress={undefined}
        start={(_host, request) => started.push(request)}
        checkRepository={(host, repository) => {
          asked.push({host, repository});
          const answer = answers[repository.trim()];
          if (answer !== undefined) setChecked({repository: repository.trim(), answer});
        }}
        retry={() => undefined}
        openRun={() => undefined}
      />
    );
  }
  const screen = mount(<Connected />);
  openRepository(screen);
  type(fieldOf(screen, 'Task'), 'decode');
  return {screen, asked, started};
};

const NOT_A_TREE = 'repository /plain is not a git work tree: it has no .git';

describe('the Start run form’s repository path as it is typed', () => {
  it('asks the host about each path typed, and again when the host is changed', () => {
    const {screen, asked} = withHostAnswers({});
    type(repository(screen), '/plain');
    type(repository(screen), '/plain/sub');
    choose(fieldOf<HTMLSelectElement>(screen, 'Host'), 'ssh:gpu-cluster-1');
    expect(asked).toEqual([
      {host: 'local', repository: '/plain'},
      {host: 'local', repository: '/plain/sub'},
      {host: 'ssh:gpu-cluster-1', repository: '/plain/sub'},
    ]);
    screen.unmount();
  });

  it('shows the host’s refusal under the field and keeps Start from sending', () => {
    const {screen, started} = withHostAnswers({'/plain': {_tag: 'Refused', reason: NOT_A_TREE}});
    type(repository(screen), '/plain');
    const field = repository(screen);
    const error = field.getAttribute('aria-describedby');
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(
      [...(error?.split(' ') ?? [])].some(
        id => screen.container.querySelector(`#${id}`)?.textContent === NOT_A_TREE,
      ),
    ).toBe(true);
    const start = screen.control('Start run');
    expect(start.hasAttribute('disabled') || start.getAttribute('aria-disabled') === 'true').toBe(
      true,
    );
    screen.press(start);
    expect(started).toEqual([]);
    screen.unmount();
  });

  it('keeps Start disabled until the host has answered for the path now in the field', () => {
    const {screen, started} = withHostAnswers({'/work/ok': {_tag: 'Passed', tasks: []}});
    type(repository(screen), '/work/o');
    screen.press(screen.control('Start run'));
    expect(started).toEqual([]);
    type(repository(screen), '/work/ok');
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(1);
    expect(started[0]?.repository).toBe('/work/ok');
    // An answer about the earlier spelling is not an answer for a new one.
    type(repository(screen), '/work/ok2');
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(1);
    screen.unmount();
  });

  it('clears the error once the path is corrected, and then starts', () => {
    const {screen, started} = withHostAnswers({
      '/plain': {_tag: 'Refused', reason: NOT_A_TREE},
      '/repo': {_tag: 'Passed', tasks: []},
    });
    type(repository(screen), '/plain');
    expect(screen.text()).toContain(NOT_A_TREE);
    type(repository(screen), '/repo');
    expect(screen.text()).not.toContain(NOT_A_TREE);
    expect(repository(screen).getAttribute('aria-invalid')).not.toBe('true');
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(1);
    screen.unmount();
  });

  it('does not hold a path back when the host could not say', () => {
    const {screen, started} = withHostAnswers({'/work/x': {_tag: 'Unasked'}});
    type(repository(screen), '/work/x');
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(1);
    screen.unmount();
  });
});

/** The folders a host lists, by the folder asked for, as the host would list them. */
const FOLDERS: Readonly<Record<string, FolderListing>> = {
  '/home/dev/': {
    _tag: 'Listed',
    truncated: false,
    entries: [
      {name: 'code', isDirectory: true, isGitRepository: false, hasTasks: false},
      {name: 'codex-notes.md', isDirectory: false, isGitRepository: false, hasTasks: false},
      {name: 'docs', isDirectory: true, isGitRepository: false, hasTasks: false},
    ],
  },
  '/home/dev/code/': {
    _tag: 'Listed',
    truncated: false,
    entries: [
      {name: 'decode-service', isDirectory: true, isGitRepository: true, hasTasks: true},
      {name: 'serving-bench', isDirectory: true, isGitRepository: true, hasTasks: false},
    ],
  },
};

/** The form over a host that lists `FOLDERS` when asked, keeping what it listed as the app does. */
const withHostFolders = (checks: Readonly<Record<string, RepositoryAnswer>> = {}) => {
  const listed: {host: string; folder: string; hidden: boolean; fresh: boolean}[] = [];
  function Connected(): ReturnType<typeof NewRun> {
    const [directories, setDirectories] = useState<ReadonlyMap<string, FolderListing>>(new Map());
    const [checked, setChecked] = useState<RepositoryChecked | undefined>(undefined);
    return (
      <NewRun
        {...NO_NEW_RUN_COMMANDS}
        hosts={HOSTS.map(host => ({...host, repositoryChecked: checked, directories}))}
        progress={undefined}
        start={() => undefined}
        checkRepository={(_host, repository) => {
          const answer = checks[repository.trim()];
          if (answer !== undefined) setChecked({repository: repository.trim(), answer});
        }}
        listDirectory={(host, folder, hidden, fresh) => {
          listed.push({host, folder, hidden, fresh});
          const listing = FOLDERS[folder];
          if (listing !== undefined && !hidden)
            setDirectories(all =>
              new Map(fresh ? [] : all).set(listingKey(folder, hidden), listing),
            );
        }}
        retry={() => undefined}
        openRun={() => undefined}
      />
    );
  }
  const screen = mount(<Connected />);
  openRepository(screen);
  return {screen, listed};
};

function key(field: HTMLInputElement, name: string): void {
  const View = field.ownerDocument.defaultView as unknown as {KeyboardEvent: typeof KeyboardEvent};
  act(() => {
    field.dispatchEvent(
      new View.KeyboardEvent('keydown', {key: name, bubbles: true, cancelable: true}),
    );
  });
}

const options = (screen: Screen): string[] =>
  [...screen.container.querySelectorAll('[role="option"]')].map(option => option.textContent ?? '');

describe('the Start run form’s completion of the repository path', () => {
  it('lists the folder the path has reached, narrows it by the typed name, and offers folders only', () => {
    const {screen, listed} = withHostFolders();
    type(repository(screen), '/home/dev/');
    expect(options(screen)).toEqual(['code', 'docs'].map(name => `/home/dev/${name}/`));
    type(repository(screen), '/home/dev/co');
    expect(options(screen)).toEqual(['/home/dev/code/']);
    type(repository(screen), '/home/dev/x');
    expect(options(screen)).toEqual([]);
    // Typing within a folder names the same folder every time: the host lists it once.
    expect(new Set(listed.map(each => each.folder))).toEqual(new Set(['/home/dev/']));
    screen.unmount();
  });

  it('marks repositories and ones with tasks by a word', () => {
    const {screen} = withHostFolders();
    type(repository(screen), '/home/dev/code/');
    expect(options(screen)).toEqual([
      '/home/dev/code/decode-service/tasks',
      '/home/dev/code/serving-bench/git',
    ]);
    screen.unmount();
  });

  it('takes the highlighted folder with Enter, appends a slash and lists the folder it names', () => {
    const {screen, listed} = withHostFolders();
    type(repository(screen), '/home/dev/');
    key(repository(screen), 'ArrowDown');
    key(repository(screen), 'ArrowDown');
    key(repository(screen), 'Enter');
    expect(repository(screen).value).toBe('/home/dev/docs/');
    expect(listed.at(-1)).toMatchObject({folder: '/home/dev/docs/', hidden: false});
    screen.unmount();
  });

  it('finishes a name with Tab, and lets Tab move on when no name is begun', () => {
    const {screen} = withHostFolders();
    type(repository(screen), '/home/dev/co');
    key(repository(screen), 'Tab');
    expect(repository(screen).value).toBe('/home/dev/code/');
    key(repository(screen), 'Tab');
    // The path ends at a slash and none is highlighted: Tab is not taken.
    expect(repository(screen).value).toBe('/home/dev/code/');
    screen.unmount();
  });

  it('asks for hidden entries only once a dot is typed, and reads afresh when the field is entered', () => {
    const {screen, listed} = withHostFolders();
    type(repository(screen), '/home/dev/');
    type(repository(screen), '/home/dev/.');
    expect(listed.map(each => each.hidden)).toEqual([false, true]);
    const field = repository(screen);
    const View = field.ownerDocument.defaultView as unknown as {FocusEvent: typeof FocusEvent};
    act(() => {
      field.dispatchEvent(new View.FocusEvent('focusin', {bubbles: true}));
    });
    expect(listed.at(-1)).toMatchObject({folder: '/home/dev/', hidden: true, fresh: true});
    screen.unmount();
  });

  it('names the tasks of a repository that passes, and keeps a line for them either way', () => {
    const {screen} = withHostFolders({
      '/home/dev/code/decode-service/': {_tag: 'Passed', tasks: ['kv-cache', 'sampler']},
    });
    const before = screen.container.querySelectorAll('.vs-help').length;
    type(repository(screen), '/home/dev/code/decode-service/');
    expect(screen.text()).toContain('Tasks: kv-cache, sampler');
    type(repository(screen), '/home/dev/');
    expect(screen.text()).not.toContain('Tasks:');
    // The line under the field is there before, during and after an answer.
    expect(screen.container.querySelectorAll('.vs-help').length).toBeGreaterThanOrEqual(before);
    screen.unmount();
  });
});
