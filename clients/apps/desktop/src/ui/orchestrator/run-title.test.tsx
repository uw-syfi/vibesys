import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  AcceleratorVendor,
  RunSchema,
  RunStatus,
  type TaskCard,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  applyChange,
  emptyState,
  initialNavigation,
  type OrchestratorState,
  readHostCost,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {servedHostCost} from '../../testing/orchestrator/served-host-cost.js';
import {CostOverviewScreen} from '../cost-overview/index.js';
import {DisplayClockProvider} from '../display-clock.js';
import {COST_OVERVIEW_WORDS, TEST_ZONE} from '../test-display-clock.js';
import {OrchestratorApp} from './app.js';
import {runTitle} from './fleet-words.js';

installDom();

const word = fc.stringMatching(/^[A-Za-z0-9][A-Za-z0-9 .-]{0,30}[A-Za-z0-9]$/);
const longWord = fc.string({minLength: 60, maxLength: 80}).filter(text => text.trim().length > 40);

const cardOf = (fields: MessageInitShape<typeof TaskCardSchema>): TaskCard =>
  create(TaskCardSchema, {task: 'folder', ...fields});

const gpuCard = fc
  .record({
    subject: fc.oneof(word, longWord),
    accelerator: word,
    count: fc.integer({min: 1, max: 4096}),
  })
  .map(({subject, accelerator, count}) => ({
    subject: subject.trim(),
    accelerator,
    count,
    card: cardOf({
      modelName: subject,
      hardware: {vendor: AcceleratorVendor.AMD, count},
      hardwareName: `AMD ${accelerator}`,
    }),
  }));

describe('runTitle', () => {
  it('is the folder name for a run recorded without a card', () => {
    fc.assert(fc.property(word, task => runTitle(null, task) === task));
  });

  it('says the subject, the count and the accelerator of a GPU card, with no vendor word', () => {
    fc.assert(
      fc.property(gpuCard, ({subject, accelerator, count, card}) => {
        const title = runTitle(card, 'folder');
        expect(title).toBe(`${subject} · ${count}× ${accelerator.trim().replace(/\s+/g, ' ')}`);
      }),
    );
  });

  it('says CPU, with no count, for a CPU card', () => {
    fc.assert(
      fc.property(word, subject => {
        const title = runTitle(
          cardOf({
            workloadName: subject,
            hardware: {vendor: AcceleratorVendor.CPU, count: 1},
            hardwareName: 'CPU',
          }),
          'folder',
        );
        expect(title).toBe(`${subject.trim()} · CPU`);
      }),
    );
  });

  it('is never empty, over cards with any of the facts missing', () => {
    const maybe = fc.option(word, {nil: ''});
    fc.assert(
      fc.property(
        maybe,
        maybe,
        maybe,
        fc.boolean(),
        fc.constantFrom(AcceleratorVendor.CPU, AcceleratorVendor.AMD, AcceleratorVendor.NVIDIA),
        (modelName, workloadName, heading, hasHardware, vendor) => {
          const title = runTitle(
            cardOf({
              modelName,
              workloadName,
              heading,
              hardwareName: 'AMD MI300X',
              ...(hasHardware ? {hardware: {vendor, count: 2}} : {}),
            }),
            'folder',
          );
          expect(title.trim()).not.toBe('');
          expect(title).not.toMatch(/ · $|^ · /);
        },
      ),
    );
  });

  it('words the examples of the design', () => {
    expect(
      runTitle(
        cardOf({
          modelName: 'Flint 40B A2.5B',
          hardware: {vendor: AcceleratorVendor.AMD, count: 1},
          hardwareName: 'AMD MI300X',
        }),
        'flint',
      ),
    ).toBe('Flint 40B A2.5B · 1× MI300X');
    expect(
      runTitle(
        cardOf({
          workloadName: 'Tokenizer throughput',
          hardware: {vendor: AcceleratorVendor.CPU, count: 1},
          hardwareName: 'CPU',
        }),
        'tokenizer',
      ),
    ).toBe('Tokenizer throughput · CPU');
  });
});

const scenario = generateScenario({seed: 5, subagents: 1, live: true});
const base: OrchestratorState = scenario.changes.reduce(applyChange, emptyState);
const flint = cardOf({
  task: 'flint',
  modelName: 'Flint 40B A2.5B',
  hardware: {vendor: AcceleratorVendor.AMD, count: 1},
  hardwareName: 'AMD MI300X',
});

/** The state with the scenario's run carrying `card` (or none), as the host stores it. */
const withCard = (card: TaskCard | undefined): OrchestratorState => ({
  ...base,
  runs: Object.fromEntries(
    Object.entries(base.runs).map(([id, run]) => [
      id,
      create(RunSchema, {...run, status: RunStatus.RUNNING, taskCard: card}),
    ]),
  ),
});

function draw(data: OrchestratorState, route = initialNavigation.runs) {
  return mount(
    <OrchestratorApp
      hosts={[hostSnapshotOf({model: {data, agentCursors: {}}})]}
      nowMs={scenario.endMs}
      commands={appCommandsOf()}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={route}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />,
  );
}

describe('a run row', () => {
  it('shows the title worded from the run card', () => {
    const screen = draw(withCard(flint));
    expect(screen.container.textContent).toContain('Flint 40B A2.5B · 1× MI300X');
    screen.unmount();
  });

  it('keeps the task name for a run with no card', () => {
    const task = Object.values(base.runs)[0]?.task ?? '';
    const screen = draw(withCard(undefined));
    expect(screen.container.textContent).toContain(task);
    expect(screen.container.textContent).not.toContain('·  ·');
    screen.unmount();
  });

  it('shows no variant word, whatever the card says of the task', () => {
    const screen = draw(withCard(create(TaskCardSchema, {...flint, task: 'flint-variant-x'})));
    expect(screen.container.textContent).not.toContain('variant');
    screen.unmount();
  });
});

describe('a run page', () => {
  it('heads the page with the title', () => {
    const runId = Object.keys(base.runs)[0] ?? '';
    const screen = draw(withCard(flint), {
      screen: 'run',
      host: 'local',
      runId,
      view: 'report',
      agentId: null,
      ideas: null,
    });
    expect(screen.container.querySelector('h1')?.textContent).toBe('Flint 40B A2.5B · 1× MI300X');
    screen.unmount();
  });
});

describe('the Cost page by run', () => {
  const served = readHostCost(servedHostCost(scenario.changes, scenario.endMs));
  const view = served._tag === 'View' ? served.view : undefined;

  const draw = (titles: Record<string, string>) =>
    mount(
      <DisplayClockProvider zone={TEST_ZONE}>
        <CostOverviewScreen
          hosts={[
            {
              key: 'local',
              label: 'This machine',
              status: {kind: 'live', cause: null, staleForMs: null},
              feed: {_tag: 'Live'},
              view,
              titles,
              started: {},
              working: new Map(),
            },
          ]}
          words={COST_OVERVIEW_WORDS}
          open={() => undefined}
          retry={() => undefined}
        />
      </DisplayClockProvider>,
    );

  it('names a run by its title where the client holds its card', () => {
    const run = view?.runs[0];
    const screen = draw({[run?.runId ?? '']: 'Flint 40B A2.5B · 1× MI300X'});
    expect(screen.container.textContent).toContain('Flint 40B A2.5B · 1× MI300X');
    expect(screen.container.textContent).not.toContain(`${run?.name}Fl`);
    screen.unmount();
  });

  it('names a run by the host name where the client holds no card for it', () => {
    const run = view?.runs[0];
    const screen = draw({});
    expect(screen.container.textContent).toContain(run?.name ?? 'missing');
    screen.unmount();
  });
});
