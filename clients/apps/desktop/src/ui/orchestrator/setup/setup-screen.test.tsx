import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {CommandSchema, RunPhase} from '@vibesys/orchestrator-proto';
import {
  answered,
  applyChange,
  applySummary,
  type CommandProgress,
  emptyState,
  initialNavigation,
  type OrchestratorState,
} from '../../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../../testing/dom/app-builders.js';
import {fieldOf, typeInto} from '../../../testing/dom/fields.js';
import {installDom, mount, type Screen} from '../../../testing/dom/harness.js';
import {type SetupFlavor, setupDaemon} from '../../../testing/orchestrator/index.js';
import {OrchestratorApp} from '../app.js';
import {completeSetupIntent, sendIntent} from '../intents.js';
import type {CompleteSetupRequest} from './complete-request.js';

installDom();

const NOW = Date.parse('2026-11-01T18:00:00Z');

/** What a window holding the daemon's log and summaries shows, with the run's history read. */
function stateOf(daemon: ReturnType<typeof setupDaemon>['daemon']): OrchestratorState {
  const state = daemon
    .summaries()
    .reduce(applySummary, daemon.changesAfter(0n).reduce(applyChange, emptyState));
  const histories = Object.fromEntries(
    Object.keys(state.runs).map(runId => [
      runId,
      {
        sequence: state.sequence,
        complete: true,
        turnsLeftOut: 0,
        checkInsLeftOut: 0,
        agentsLeftOut: 0,
      },
    ]),
  );
  return {...state, histories};
}

/** The page of the setup run over a fake daemon: commands go to it, and what it holds is what is shown. */
function openSetup(flavor: SetupFlavor) {
  const {daemon, world} = setupDaemon(flavor);
  const runId = daemon.scenario.runId;
  const sent: string[] = [];
  const stops: string[] = [];
  let progress: CommandProgress | undefined;
  let screen: Screen | undefined;
  let count = 0;
  const app = () => (
    <OrchestratorApp
      hosts={[hostSnapshotOf({model: {data: stateOf(daemon), agentCursors: {}}})]}
      nowMs={NOW}
      commands={appCommandsOf({
        complete: progress === undefined ? {} : {[runId]: progress},
        sendMessage: (_host, agentId, text) => {
          sent.push(text);
          count += 1;
          daemon.dispatch(
            create(CommandSchema, {id: `send-${count}`, body: sendIntent(agentId, text)}),
          );
          screen?.render(app());
        },
        completeSetup: (_host, id, request: CompleteSetupRequest) => {
          count += 1;
          const command = create(CommandSchema, {
            id: `complete-${count}`,
            body: completeSetupIntent(id, request),
          });
          progress = answered(daemon.dispatch(command));
          screen?.render(app());
        },
        stopRun: (_host, id) => {
          stops.push(id);
        },
      })}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={{screen: 'run', host: 'local', runId, view: 'report', agentId: null, ideas: null}}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />
  );
  const mounted = mount(app());
  screen = mounted;
  const say = (text: string): void => {
    typeInto(fieldOf(mounted, 'Your message'), text);
    mounted.press(mounted.control('Send message'));
  };
  const start = (): void => mounted.press(mounted.control('Start run'));
  const startDisabled = (): boolean =>
    mounted.control('Start run').getAttribute('aria-disabled') === 'true';
  const rows = (): number => mounted.container.querySelectorAll('[data-even]').length;
  const run = () => stateOf(daemon).runs[runId];
  const refresh = (): void => mounted.render(app());
  return {
    screen: mounted,
    daemon,
    world,
    say,
    start,
    startDisabled,
    rows,
    run,
    refresh,
    sent,
    stops,
  };
}

const CHECKS = [
  'Objective',
  'Correctness check',
  'Benchmark',
  'Base code passes',
  'Broken code fails',
  'Number is stable',
] as const;

/** Whether the page says `status` against the check `label`. */
const reads = (screen: Screen, label: string, status: string): boolean =>
  screen.text().includes(`${label}${status}`);

/** Whether the page says its facts are as of the last read: the word is shown, not only held in reserve. */
const saysLastRead = (screen: Screen): boolean =>
  [...screen.container.querySelectorAll('.vs-chip')].some(
    chip => chip.textContent === 'As of last read' && chip.closest('.vs-reserve') === null,
  );

describe('the setup page', () => {
  it('is the chat and the draft: six pending checks, Start closed, the lead asking its first question', () => {
    const {screen, startDisabled, rows} = openSetup('ticks');
    expect(screen.text()).toContain('Setting up');
    expect(screen.text()).toContain('decode-service');
    expect(screen.text()).toContain('What should get faster');
    for (const label of CHECKS) expect(reads(screen, label, 'Pending')).toBe(true);
    expect(rows()).toBe(6);
    expect(startDisabled()).toBe(true);
  });

  it('ticks each check from Pending to Passed as the lead works, with six rows at every step', () => {
    const {screen, say, startDisabled, rows} = openSetup('ticks');
    const seen: number[] = [];
    for (const message of ['a', 'b', 'c', 'd', 'e']) {
      say(message);
      seen.push(rows());
    }
    expect(seen).toEqual([6, 6, 6, 6, 6]);
    for (const label of CHECKS) expect(reads(screen, label, 'Passed')).toBe(true);
    expect(screen.text()).toContain('21.4');
    expect(startDisabled()).toBe(false);
  });

  it('shows a check that is running as Running before it passes', () => {
    const {screen, say} = openSetup('ticks');
    say('a');
    say('b');
    expect(reads(screen, 'Base code passes', 'Running')).toBe(true);
    expect(reads(screen, 'Correctness check', 'Passed')).toBe(true);
  });

  it('starts the run when every check passed: the page becomes the optimize page', () => {
    const {screen, say, start, run} = openSetup('ticks');
    for (const message of ['a', 'b', 'c', 'd', 'e']) say(message);
    start();
    expect(run()?.phase).toBe(RunPhase.OPTIMIZE);
    expect(screen.text()).not.toContain('Task draft');
    expect(screen.controls('Start run')).toHaveLength(0);
    expect(screen.controls('Catch me up')).toHaveLength(1);
  });

  it('keeps the facts in place, quieter and said so, when the host could not read; and clears when it can', () => {
    const {screen, say} = openSetup('unreadable');
    say('a');
    say('b');
    expect(saysLastRead(screen)).toBe(false);
    say('c');
    expect(saysLastRead(screen)).toBe(true);
    expect(reads(screen, 'Correctness check', 'Passed')).toBe(true);
    say('d');
    expect(saysLastRead(screen)).toBe(false);
    expect(reads(screen, 'Number is stable', 'Running')).toBe(true);
  });

  it('names the unmet check inline when the host refuses Start, and starts after the host reads it passed', () => {
    const {screen, say, start, startDisabled, run} = openSetup('refused');
    for (const message of ['a', 'b', 'c', 'd', 'e']) say(message);
    expect(startDisabled()).toBe(false);
    start();
    expect(screen.text()).toContain('Not passed: Number is stable');
    expect(run()?.phase).toBe(RunPhase.SETUP);
    expect(reads(screen, 'Number is stable', 'Failed')).toBe(true);
    expect(startDisabled()).toBe(true);
    say('measure again');
    expect(startDisabled()).toBe(false);
    start();
    expect(run()?.phase).toBe(RunPhase.OPTIMIZE);
  });

  it('says Starting as run state while the lead finishes its turn, and swaps when it rests', () => {
    const {screen, world, say, start, run, refresh, startDisabled} = openSetup('pending');
    for (const message of ['a', 'b', 'c', 'd', 'e']) say(message);
    start();
    expect(screen.text()).toContain('Starting');
    expect(startDisabled()).toBe(true);
    expect(run()?.phase).toBe(RunPhase.SETUP);
    world.restLead(run()?.id ?? '', NOW);
    refresh();
    expect(run()?.phase).toBe(RunPhase.OPTIMIZE);
    expect(screen.text()).not.toContain('Task draft');
  });

  it('is listed on Runs as Setting up with the repository name, and a run that started is listed as its task', () => {
    const {daemon, say, start} = openSetup('ticks');
    const list = () =>
      mount(
        <OrchestratorApp
          hosts={[hostSnapshotOf({model: {data: stateOf(daemon), agentCursors: {}}})]}
          nowMs={NOW}
          commands={appCommandsOf()}
          retryHost={() => undefined}
          retryCost={() => undefined}
          route={initialNavigation.runs}
          fleet={initialNavigation.fleet}
          go={() => undefined}
        />,
      );
    const before = list();
    expect(before.text()).toContain('Setting up \u00b7 decode-service');
    expect(before.text()).toContain('Setting up');
    before.unmount();
    for (const message of ['a', 'b', 'c', 'd', 'e']) say(message);
    start();
    const after = list();
    expect(after.text()).not.toContain('Setting up');
    expect(after.text()).toContain('Ring buffer');
    after.unmount();
  });

  it('is stoppable', () => {
    const {screen, stops, run} = openSetup('ticks');
    screen.press(screen.control('Stop run'));
    expect(stops).toEqual([run()?.id]);
  });
});
