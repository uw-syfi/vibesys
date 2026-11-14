import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  CatalogueProblemReason,
  EvalPlaceKind,
  EvalPlaceSchema,
  ListEvalPlacesResponseSchema,
  ReceiptSchema,
  RunSchema,
  RunStatus,
} from '@vibesys/orchestrator-proto';
import {act, useState} from 'react';
import {
  applyChange,
  emptyState,
  initialNavigation,
  type Navigation,
  type NavigationAction,
  navigationReducer,
  REGISTERED_EXAMPLES,
  type RepositoryChecked,
  readOf,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {richCatalogueResponse, richPlaces} from '../../testing/orchestrator/new-run-catalogue.js';
import {fixtureCatalogueProblemResponse} from '../../testing/orchestrator/new-run-fixtures.js';
import type {AppCommands, HostSnapshot} from './app.js';
import {OrchestratorApp} from './app.js';

installDom();

const PLACES = create(ListEvalPlacesResponseSchema, {
  places: [...richPlaces(), create(EvalPlaceSchema, {name: 'modal', kind: EvalPlaceKind.CLOUD})],
});

const read = (overrides: Partial<HostSnapshot> = {}) =>
  hostSnapshotOf({
    catalogue: readOf(richCatalogueResponse()),
    evalPlaces: readOf(PLACES),
    ...overrides,
  });

/** The app as a person has it: the route is the navigation's, and every move is applied to it. */
function Window(props: {
  readonly hosts: readonly HostSnapshot[];
  readonly commands: AppCommands;
  readonly seen: NavigationAction[];
  readonly initial?: Navigation;
}) {
  const [navigation, setNavigation] = useState<Navigation>(props.initial ?? initialNavigation);
  return (
    <OrchestratorApp
      hosts={props.hosts}
      nowMs={0}
      commands={props.commands}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={navigation.runs}
      fleet={navigation.fleet}
      go={action => {
        props.seen.push(action);
        setNavigation(navigationReducer(navigation, action));
      }}
    />
  );
}

/** Opens the page from the Runs list's New run button. */
function open(
  hosts: readonly HostSnapshot[] = [read()],
  overrides: Partial<AppCommands> = {},
): {readonly screen: Screen; readonly seen: NavigationAction[]} {
  const seen: NavigationAction[] = [];
  const screen = mount(<Window hosts={hosts} commands={appCommandsOf(overrides)} seen={seen} />);
  screen.press(screen.control('New run'));
  return {screen, seen};
}

/** A card, or any control, named by text it contains: kickers may precede a card's title. */
function card(screen: Screen, words: string): HTMLElement {
  const found = [...screen.container.querySelectorAll<HTMLElement>('button')].find(each =>
    (each.textContent ?? '').includes(words),
  );
  if (found === undefined) throw new Error(`no card containing ${words}`);
  return found;
}

/** The radio a segment's label names: a segmented control is native radios the harness does not list. */
function segment(screen: Screen, label: string): HTMLInputElement {
  const named = [...screen.container.querySelectorAll('label')].find(each =>
    (each.textContent ?? '').trim().startsWith(label),
  );
  const radio = named?.querySelector('input');
  if (radio === null || radio === undefined) throw new Error(`no segment ${label}`);
  return radio;
}

/** Types into a field as a person does: focused, the native value setter, then input and change events. */
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

const field = (screen: Screen, placeholder: string): HTMLInputElement => {
  const found = screen.container.querySelector<HTMLInputElement>(
    `input[placeholder="${placeholder}"]`,
  );
  if (found === null) throw new Error(`no field ${placeholder}`);
  return found;
};

const PASSED = (tasks: string[]): RepositoryChecked => ({
  repository: '/work/decode-service',
  answer: {_tag: 'Passed', tasks},
});

describe('the New run page', () => {
  it('opens from the Runs list as a page with its trail, the machine strip and two entry cards', () => {
    const loaded: string[] = [];
    const {screen, seen} = open([read(), read({key: 'ssh:host-a', label: 'host-a'})], {
      loadNewRun: host => loaded.push(host),
    });
    expect(seen).toEqual([{type: 'open-new-run'}]);
    expect(loaded).toEqual(['local', 'ssh:host-a']);
    const text = screen.text();
    expect(text).toContain('New run');
    expect(text).toContain('Start from an example');
    expect(text).toContain('Bring a repository, or describe one');
    // A tab per machine, each with the hardware it can evaluate on: the cloud place is left out.
    expect(text).toContain('This machine · CPU · ');
    expect(text).toContain('host-a · CPU · ');
    expect(text).toContain('Evals on');
    expect(text).toContain('Local Docker');
    expect(text).not.toContain('modal');
    expect(screen.controls('Add machine or server')).toHaveLength(1);
    screen.unmount();
  });

  it('sends Add machine or server to Machines, and the Runs crumb back to the list', () => {
    const {screen, seen} = open();
    screen.press(screen.control('Add machine or server'));
    expect(seen.at(-1)).toEqual({type: 'open-add-machine', origin: 'new-run'});
    screen.unmount();
    const again = open();
    again.screen.press(again.screen.control('Runs'));
    expect(again.seen.at(-1)).toEqual({type: 'open-runs'});
    again.screen.unmount();
  });

  it('opens fresh: it clears the last start and each machine’s copy, and lists the folder again', () => {
    const cleared: string[] = [];
    const listed: boolean[] = [];
    const screen = mount(
      <Window
        hosts={[read({repositoriesDirectory: '/srv/repos'})]}
        commands={appCommandsOf({
          start: {_tag: 'Accepted', receipt: create(ReceiptSchema, {runId: 'run-9'})},
          clearStart: () => cleared.push('start'),
          forgetProvision: host => cleared.push(host),
          listDirectory: (_host, _folder, _hidden, fresh) => listed.push(fresh),
        })}
        seen={[]}
      />,
    );
    screen.press(screen.control('New run'));
    expect(cleared).toEqual(['start', 'local']);
    expect(listed).toEqual([true]);
    screen.unmount();
  });

  it('opens fresh from a failed report through Run again, using the same preparation as New run', () => {
    const scenario = generateScenario({seed: 8, subagents: 1, live: false});
    const data = scenario.changes.reduce(applyChange, emptyState);
    const run = data.runs[scenario.runId];
    if (run === undefined) throw new Error('the fixture has its run');
    const failed = {
      ...data,
      runs: {
        ...data.runs,
        [scenario.runId]: create(RunSchema, {...run, status: RunStatus.FAILED}),
      },
    };
    const cleared: string[] = [];
    const loaded: string[] = [];
    const listed: string[] = [];
    const seen: NavigationAction[] = [];
    const screen = mount(
      <Window
        hosts={[
          read({model: {data: failed, agentCursors: {}}, repositoriesDirectory: '/srv/repos/'}),
          read({key: 'ssh:host-a', label: 'host-a'}),
        ]}
        commands={appCommandsOf({
          clearStart: () => cleared.push('start'),
          forgetProvision: host => cleared.push(host),
          loadNewRun: host => loaded.push(host),
          listDirectory: (host, folder) => listed.push(`${host}:${folder}`),
        })}
        seen={seen}
        initial={{
          ...initialNavigation,
          runs: {
            screen: 'run',
            host: 'local',
            runId: scenario.runId,
            view: 'report',
            agentId: null,
            ideas: null,
          },
        }}
      />,
    );
    screen.press(screen.control('Run again'));
    expect(seen).toEqual([{type: 'open-new-run'}]);
    expect(cleared).toEqual(['start', 'local', 'ssh:host-a']);
    expect(loaded).toEqual(['local', 'ssh:host-a']);
    expect(listed).toEqual(['local:/srv/repos/']);
    expect(screen.text()).toContain('New run');
    screen.unmount();
  });

  it('shows a window that closes and opens again as a new page: the drafts go with the page', () => {
    const {screen} = open();
    screen.press(card(screen, 'CatalogStart from an example'));
    const first = screen.container.querySelector<HTMLElement>(
      '.vs-pick-card:not([data-unavailable])',
    );
    if (first === null) throw new Error('a card fits');
    screen.press(first);
    expect(screen.text()).toContain('Start run');
    screen.press(screen.controls('Runs').find(c => c.classList.contains('vs-crumb')) ?? first);
    screen.press(screen.control('New run'));
    expect(screen.text()).toContain('Bring a repository, or describe one');
    expect(screen.text()).not.toContain('Start run');
    screen.unmount();
  });

  it('points to Machines, with a status word, when no eval place can be read', () => {
    const none = create(ListEvalPlacesResponseSchema, {places: []});
    const {screen, seen} = open([read({evalPlaces: readOf(none)})]);
    screen.press(screen.control('Set up an eval place'));
    expect(seen.at(-1)).toEqual({type: 'open-add-machine', origin: 'new-run'});
    screen.unmount();
    const ok = open();
    expect(ok.screen.controls('Set up an eval place')).toHaveLength(0);
    ok.screen.unmount();
  });

  it('picks another machine by its tab, and reads it', () => {
    const loaded: string[] = [];
    const {screen} = open([read(), read({key: 'ssh:host-a', label: 'host-a'})], {
      loadNewRun: host => loaded.push(host),
    });
    loaded.length = 0;
    act(() => segment(screen, 'host-a').click());
    expect(loaded).toEqual(['ssh:host-a']);
    screen.unmount();
  });

  it('keeps showing the machine before while the picked one is read, dimmed, and swaps once', () => {
    const before = read({key: 'ssh:host-a', label: 'host-a'});
    const reading = hostSnapshotOf({
      key: 'ssh:host-b',
      label: 'host-b',
      catalogue: {data: null, reading: true, failure: null},
      evalPlaces: {data: null, reading: true, failure: null},
    });
    const seen: NavigationAction[] = [];
    const screen = mount(
      <Window hosts={[before, reading]} commands={appCommandsOf()} seen={seen} />,
    );
    screen.press(screen.control('New run'));
    act(() => segment(screen, 'host-b').click());
    // host-b is being read: host-a's eval places are still there, dimmed.
    expect(screen.container.querySelector('[data-stale]')).not.toBeNull();
    expect(screen.text()).toContain('Local Docker');
    expect(screen.text()).not.toContain('catalog tasks fit');
    screen.render(
      <Window
        hosts={[before, read({key: 'ssh:host-b', label: 'host-b'})]}
        commands={appCommandsOf()}
        seen={seen}
      />,
    );
    expect(screen.container.querySelector('[data-stale]')).toBeNull();
    screen.unmount();
  });
});

describe('the catalog step', () => {
  it('browses the complete VibeSys registry before any machine is connected', () => {
    const {screen} = open([]);
    screen.press(card(screen, 'CatalogStart from an example'));
    expect(screen.text()).toContain('No machine connected. You can still browse every example.');
    expect(screen.text()).toContain(
      `${REGISTERED_EXAMPLES.length} of ${REGISTERED_EXAMPLES.length} examples`,
    );
    expect(screen.text()).toContain('Browsing offline · connect a machine to start');
    expect(screen.text()).toContain('Model serving');
    expect(screen.text()).toContain('Data structures');
    expect(screen.container.querySelectorAll('.vs-pick-card')).toHaveLength(
      REGISTERED_EXAMPLES.length,
    );
    screen.press(card(screen, 'queue-rs'));
    expect(screen.text()).toContain('Connect a machine that provides it');
    expect(screen.text()).toContain('git clone https://github.com/uw-syfi/vibesys.git');
    expect(screen.controls('Start run')).toHaveLength(0);
    screen.unmount();
  });

  it('lists the examples by domain as cards with a title, an id line and objective chips', () => {
    const {screen} = open();
    screen.press(card(screen, 'CatalogStart from an example'));
    const text = screen.text();
    expect(text).toContain('Start from an example');
    expect(text).toContain('Catalog');
    expect(text).toContain('Model serving');
    expect(text).toContain('Data structures');
    expect(text).toMatch(/\d+ of \d+ examples/);
    expect(text).not.toContain('Fits ·');
    expect(text).not.toContain('All ·');
    // The id line is the example's folder and its task.
    expect(text).toMatch(/\w+ \/ \w+/);
    const split = screen.container.querySelector('.vs-split');
    expect(split?.firstElementChild?.classList.contains('vs-split-aside')).toBe(true);
    expect(split?.hasAttribute('data-aside-at-start')).toBe(true);
    screen.unmount();
  });

  it('shows every example with compatibility in place, then narrows by search', () => {
    const {screen} = open();
    screen.press(card(screen, 'CatalogStart from an example'));
    const all = screen.container.querySelectorAll('.vs-pick-card').length;
    const unavailable = screen.container.querySelectorAll('.vs-pick-card[data-unavailable]').length;
    expect(all).toBeGreaterThan(0);
    expect(unavailable).toBeGreaterThan(0);
    expect(unavailable).toBeLessThan(all);
    type(field(screen, 'Example, model, hardware, task'), 'zzz-no-such-example');
    expect(screen.container.querySelectorAll('.vs-pick-card')).toHaveLength(0);
    screen.unmount();
  });

  it('copies an example and starts in it: the example, a place it fits, the name and no variant', () => {
    const calls: {host: string; example: string; name: string; place: string; task: string}[] = [];
    const {screen} = open([read()], {
      startFromExample: (host, example, request) =>
        calls.push({
          host,
          example: example.example,
          name: example.name,
          place: request.evalPlace,
          task: request.task,
        }),
    });
    screen.press(card(screen, 'CatalogStart from an example'));
    const cards = [
      ...screen.container.querySelectorAll<HTMLElement>('.vs-pick-card:not([data-unavailable])'),
    ];
    const first = cards[0];
    if (first === undefined) throw new Error('the catalogue has a card that fits');
    screen.press(first);
    expect(screen.text()).toContain('Evals on');
    // The run's bounds are on the panel; the model is under More options.
    expect(screen.text()).toContain('Time limit');
    expect(screen.text()).toContain('Cost limit ($)');
    screen.press(screen.control('More options'));
    expect(screen.text()).toContain('Model');
    screen.press(screen.control('Start run'));
    expect(calls).toHaveLength(1);
    expect(calls[0]?.host).toBe('local');
    expect(calls[0]?.place).not.toBe('modal');
    expect(calls[0]?.example).not.toBe('');
    screen.unmount();
  });

  it('says a refused copy beside Start, not only inside More options', () => {
    const screen = mount(
      <Window
        hosts={[
          read({
            provision: {_tag: 'Refused', reason: 'NEEDS_NETWORK', detail: 'cloned', existing: ''},
          }),
        ]}
        commands={appCommandsOf()}
        seen={[]}
      />,
    );
    screen.press(screen.control('New run'));
    screen.press(card(screen, 'CatalogStart from an example'));
    const first = screen.container.querySelector<HTMLElement>(
      '.vs-pick-card:not([data-unavailable])',
    );
    if (first === null) throw new Error('a card fits');
    screen.press(first);
    expect(screen.text()).toContain('Needs network');
    screen.unmount();
  });

  it('goes back one step by the New run crumb', () => {
    const {screen} = open();
    screen.press(card(screen, 'CatalogStart from an example'));
    screen.press(screen.control('New run'));
    expect(screen.text()).toContain('Bring a repository, or describe one');
    screen.unmount();
  });

  it('shows the fix for a classified catalogue problem instead of the card grid', () => {
    const problem = fixtureCatalogueProblemResponse(
      CatalogueProblemReason.NO_RELEASE,
      'no release found next to this host',
    );
    const {screen} = open([read({catalogue: readOf(problem)})]);
    screen.press(card(screen, 'CatalogStart from an example'));
    const text = screen.text();
    expect(text).toContain('no release found next to this host');
    expect(text).toContain('install.sh');
    expect(text).toContain('vibesys-client host status');
    expect(screen.container.querySelectorAll('.vs-pick-card')).toHaveLength(0);
    screen.unmount();
  });

  it('shows the host failure message for an unclassified catalogue failure', () => {
    const {screen} = open([
      read({catalogue: {data: null, reading: false, failure: 'the daemon is down'}}),
    ]);
    screen.press(card(screen, 'CatalogStart from an example'));
    expect(screen.text()).toContain('the daemon is down');
    screen.unmount();
  });
});

describe('the My own step', () => {
  const ownHost = () => read({repositoryChecked: PASSED(['decode', 'prefill'])});
  const toOwn = (overrides: Partial<AppCommands> = {}) => {
    const opened = open([ownHost()], overrides);
    opened.screen.press(card(opened.screen, 'My ownBring a repository'));
    return opened;
  };

  it('offers the three cases', () => {
    const {screen} = toOwn();
    const text = screen.text();
    expect(text).toContain('A repository with a task');
    expect(text).toContain('A repository without a task');
    expect(text).toContain('Nothing yet');
    screen.unmount();
  });

  it('starts a task of a repository that has some', () => {
    const started: {host: string; repository: string; task: string}[] = [];
    const {screen} = toOwn({
      startRun: (host, request) =>
        started.push({host, repository: request.repository, task: request.task}),
    });
    screen.press(card(screen, 'A repository with a task'));
    type(field(screen, 'Pick or type a path'), '/work/decode-service');
    expect(screen.text()).toContain('Git repository, 2 tasks');
    expect(screen.container.querySelector('.vs-help[data-tone="ok"]')?.textContent).toBe(
      'Git repository, 2 tasks',
    );
    screen.press(card(screen, 'prefill'));
    screen.press(screen.control('Start run'));
    expect(started).toEqual([{host: 'local', repository: '/work/decode-service', task: 'prefill'}]);
    screen.unmount();
  });

  it('closes the completion list once the path names a repository, and starts each case with an empty path', () => {
    const {screen} = toOwn();
    screen.press(card(screen, 'A repository with a task'));
    type(field(screen, 'Pick or type a path'), '/work/decode-service');
    expect(screen.container.querySelector('[role=option]')).toBeNull();
    screen.press(card(screen, 'A repository without a task'));
    expect(field(screen, '~/code/my-server').value).toBe('');
    screen.unmount();
  });

  it('begins a setup chat in a repository without a task', () => {
    const begun: {host: string; repository: string}[] = [];
    const {screen} = toOwn({
      beginSetup: (host, request) => begun.push({host, repository: request.repository}),
    });
    screen.press(card(screen, 'A repository without a task'));
    type(field(screen, '~/code/my-server'), '/work/decode-service');
    screen.press(screen.control('Set up a task'));
    expect(begun).toEqual([{host: 'local', repository: '/work/decode-service'}]);
    screen.unmount();
  });

  it('makes the repository first when there is nothing yet, then begins the setup', () => {
    const made: {host: string; name: string; parent: string}[] = [];
    const {screen} = toOwn({
      setUpInNewRepository: (host, repository) =>
        made.push({host, name: repository.name, parent: repository.parent}),
    });
    screen.press(card(screen, 'Nothing yet'));
    screen.press(screen.control('Set up a task'));
    expect(made).toEqual([]);
    type(field(screen, 'ring-buffer'), ' ring-buffer ');
    screen.press(screen.control('Set up a task'));
    expect(made).toEqual([{host: 'local', name: 'ring-buffer', parent: ''}]);
    screen.unmount();
  });

  it('opens the run a start created', () => {
    const seen: NavigationAction[] = [];
    const accepted = appCommandsOf({
      start: {_tag: 'Accepted', receipt: create(ReceiptSchema, {runId: 'run-9'})},
    });
    const screen = mount(<Window hosts={[ownHost()]} commands={accepted} seen={seen} />);
    screen.press(screen.control('New run'));
    screen.press(card(screen, 'My ownBring a repository'));
    screen.press(screen.control('Open the run'));
    expect(seen.at(-1)).toEqual({type: 'open-run', host: 'local', runId: 'run-9'});
    screen.unmount();
  });
});
