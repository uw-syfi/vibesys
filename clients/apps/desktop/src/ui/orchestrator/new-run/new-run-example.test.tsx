import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  CatalogueEntrySchema,
  FitSchema,
  FitState,
  ListCatalogueResponseSchema,
  TaskCardSchema,
  TaskDomain,
} from '@vibesys/orchestrator-proto';
import {act} from 'react';
import type {ProvisionExample} from '../../../core/index.js';
import {initialNavigation, type ProvisionProgress, readOf} from '../../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../../testing/dom/app-builders.js';
import {installDom, mount, type Screen} from '../../../testing/dom/harness.js';
import {
  fixtureCatalogueResponse,
  fixturePlacesResponse,
} from '../../../testing/orchestrator/new-run-fixtures.js';
import {type AppCommands, type HostSnapshot, OrchestratorApp} from '../app.js';
import type {StartRunRequest} from '../start-draft.js';

installDom();

interface Started {
  readonly host: string;
  readonly example: ProvisionExample;
  readonly request: StartRunRequest;
}

function open(
  host: Partial<HostSnapshot> = {},
  commands: Partial<AppCommands> = {},
): {screen: Screen; started: Started[]; loaded: string[]} {
  const started: Started[] = [];
  const loaded: string[] = [];
  const screen = mount(
    <OrchestratorApp
      hosts={[
        hostSnapshotOf({
          catalogue: readOf(fixtureCatalogueResponse()),
          evalPlaces: readOf(fixturePlacesResponse()),
          ...host,
        }),
      ]}
      nowMs={0}
      commands={appCommandsOf({
        loadNewRun: key => loaded.push(key),
        startFromExample: (key, example, request) => started.push({host: key, example, request}),
        ...commands,
      })}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={initialNavigation.runs}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />,
  );
  screen.press(screen.control('New run'));
  return {screen, started, loaded};
}

const View = (element: Element) =>
  element.ownerDocument.defaultView as unknown as {Event: typeof Event};

function type(field: HTMLInputElement, text: string): void {
  const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field) as object, 'value')?.set;
  if (set === undefined) throw new Error('a text field has a value setter');
  act(() => {
    field.focus();
    set.call(field, text);
    for (const name of ['input', 'change', 'keyup'])
      field.dispatchEvent(new (View(field).Event)(name, {bubbles: true}));
  });
}

function choose(select: HTMLSelectElement, value: string): void {
  act(() => {
    select.value = value;
    select.dispatchEvent(new (View(select).Event)('change', {bubbles: true}));
  });
}

function fieldOf<T extends HTMLElement>(screen: Screen, label: string): T {
  const named = [...screen.container.querySelectorAll('label')].find(
    each => each.textContent?.trim() === label,
  );
  const control =
    named === undefined ? null : screen.container.querySelector<T>(`#${named.htmlFor}`);
  if (control === null) throw new Error(`no field named ${label}`);
  return control;
}

const segment = (screen: Screen, label: string): HTMLInputElement => {
  const dialog = screen.container.querySelector('dialog');
  const named = [...(dialog?.querySelectorAll('label') ?? [])].find(
    each => each.textContent?.trim() === label,
  );
  const radio = named?.querySelector('input');
  if (radio === null || radio === undefined) throw new Error(`no segment ${label}`);
  return radio;
};

const searchOf = (screen: Screen): HTMLInputElement => {
  const field = screen.container.querySelector<HTMLInputElement>('input[aria-label="Search"]');
  if (field === null) throw new Error('no search');
  return field;
};

const row = (screen: Screen, title: string): HTMLElement => {
  const found = [...screen.container.querySelectorAll<HTMLElement>('[role="option"]')].find(each =>
    each.textContent?.includes(title),
  );
  if (found === undefined) throw new Error(`no row ${title}`);
  return found;
};

const rows = (screen: Screen): string[] =>
  [...screen.container.querySelectorAll('[role="option"]')].map(each => each.textContent ?? '');

describe('New run from the host’s catalogue', () => {
  it('asks the host for its catalogue and places when it is opened', () => {
    const {screen, loaded} = open();
    expect(loaded).toEqual(['local']);
    screen.unmount();
  });

  it('lists the entries the host says fit, grouped by domain, each with its verdict', () => {
    const {screen} = open();
    const text = screen.container.textContent ?? '';
    expect(text).toContain('Model serving');
    expect(text).toContain('Data structures');
    expect(text).toContain('Databases');
    expect(text.indexOf('Model serving')).toBeLessThan(text.indexOf('Data structures'));
    expect(text.indexOf('Data structures')).toBeLessThan(text.indexOf('Databases'));
    expect(rows(screen).every(each => each.includes('Fits'))).toBe(true);
    expect(rows(screen).some(each => each.includes('sparrow'))).toBe(false);
    screen.unmount();
  });

  it('shows the whole catalogue on All, the host’s word on each, and the folder name of each', () => {
    const {screen} = open();
    screen.press(segment(screen, 'All \u00b7 5'));
    const listed = rows(screen);
    expect(listed).toHaveLength(5);
    expect(row(screen, 'sparrow').textContent).toContain('Does not fit');
    expect(row(screen, 'flint').textContent).toContain('Fits');
    screen.unmount();
  });

  it('narrows by what is typed', () => {
    const {screen} = open();
    type(searchOf(screen), 'swmr');
    expect(rows(screen)).toHaveLength(1);
    expect(rows(screen)[0]).toContain('swmr-map');
    screen.unmount();
  });

  it('picks the first fitting place, starts on it, and copies where the person says', () => {
    const {screen, started} = open();
    screen.press(row(screen, 'swmr-map'));
    expect(fieldOf<HTMLSelectElement>(screen, 'Evals on').value).toBe('local');
    type(fieldOf<HTMLInputElement>(screen, 'Copy to'), '~/work');
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(1);
    expect(started[0]).toMatchObject({
      host: 'local',
      example: {example: 'data-structures/swmr-map', name: 'swmr-map', parent: '~/work'},
      request: {task: 'swmr-map', evalPlace: 'local'},
    });
    screen.unmount();
  });

  it('puts the select on the place the host named for a GPU task, and lets the person move it', () => {
    const {screen, started} = open();
    screen.press(row(screen, 'flint'));
    const select = fieldOf<HTMLSelectElement>(screen, 'Evals on');
    expect(select.value).toBe('cluster-a');
    expect([...select.options].find(each => each.value === 'host-a')?.disabled).toBe(true);
    choose(select, 'cluster-a');
    screen.press(screen.control('Start run'));
    expect(started[0]?.request.evalPlace).toBe('cluster-a');
    screen.unmount();
  });

  it('does not start an example the host says does not fit, and says its word', () => {
    const {screen, started} = open();
    screen.press(segment(screen, 'All \u00b7 5'));
    screen.press(row(screen, 'sparrow'));
    const start = screen.control('Start run');
    expect(start.hasAttribute('disabled') || start.getAttribute('aria-disabled') === 'true').toBe(
      true,
    );
    expect(screen.container.textContent).toContain('Does not fit');
    screen.press(start);
    expect(started).toHaveLength(0);
    screen.unmount();
  });

  it('says places unreadable when the host could not read them, and starts nothing', () => {
    const unreadable = create(ListCatalogueResponseSchema, {
      release: 'r',
      entries: [
        create(CatalogueEntrySchema, {
          example: 'databases/kv-store',
          card: create(TaskCardSchema, {task: 'kv-store', domain: TaskDomain.DATABASES}),
          fit: create(FitSchema, {state: FitState.PLACES_UNREADABLE}),
        }),
      ],
    });
    const {screen, started} = open({catalogue: readOf(unreadable)});
    screen.press(segment(screen, 'All \u00b7 1'));
    expect(row(screen, 'kv-store').textContent).toContain('Places unreadable');
    screen.press(row(screen, 'kv-store'));
    screen.press(screen.control('Start run'));
    expect(started).toHaveLength(0);
    screen.unmount();
  });

  it('shows the copy in its own line, by a status word, and holds Start while it copies', () => {
    const refused: ProvisionProgress = {
      _tag: 'Refused',
      reason: 'NAME_TAKEN',
      detail: 'swmr-map exists',
      existing: '/r/swmr-map',
    };
    const taken = open({provision: refused});
    expect(taken.screen.container.textContent).toContain('Name taken');
    taken.screen.unmount();
    const copying = open({provision: {_tag: 'Working'}});
    copying.screen.press(row(copying.screen, 'swmr-map'));
    copying.screen.press(copying.screen.control('Start run'));
    expect(copying.started).toHaveLength(0);
    expect(copying.screen.container.textContent).toContain('Copying');
    copying.screen.unmount();
  });

  it('keeps one line under the folder, whatever the copy says, so nothing moves', () => {
    const states: ProvisionProgress[] = [
      {_tag: 'Idle'},
      {_tag: 'Working'},
      {_tag: 'Done', repository: '/r/swmr-map'},
      {_tag: 'Refused', reason: 'COPY_FAILED', detail: 'disk full', existing: ''},
      {_tag: 'Failed', detail: 'no answer'},
    ];
    for (const provision of states) {
      const {screen} = open({provision});
      const field = fieldOf<HTMLInputElement>(screen, 'Copy to').closest('.vs-field');
      expect(field?.querySelectorAll('.vs-help')).toHaveLength(1);
      screen.unmount();
    }
  });
});
