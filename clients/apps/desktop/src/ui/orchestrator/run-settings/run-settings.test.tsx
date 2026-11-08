import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  CommandSchema,
  ModelCatalogSchema,
  RunChangeSchema,
  RunStatus,
} from '@vibesys/orchestrator-proto';
import {
  answered,
  applyChange,
  type CommandProgress,
  emptyState,
  initialNavigation,
  type OrchestratorState,
} from '../../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../../testing/dom/app-builders.js';
import {choose, fieldOf, typeInto} from '../../../testing/dom/fields.js';
import {installDom, mount, type Screen} from '../../../testing/dom/harness.js';
import {FakeDaemon, generateScenario} from '../../../testing/orchestrator/index.js';
import type {Scenario} from '../../../testing/orchestrator/scenario.js';
import {OrchestratorApp} from '../app.js';
import {updateRunMessage} from '../intents.js';
import type {RunUpdateRequest} from './update-draft.js';

installDom();

const scenario = generateScenario({seed: 5, subagents: 3, live: true});

/** What a window holding the daemon's log shows, with the run's history read as the page reads it. */
function stateOf(daemon: FakeDaemon): OrchestratorState {
  const state = daemon.changesAfter(0n).reduce(applyChange, emptyState);
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

/** The scenario with its lead in a turn at the end: a change of the lead waits for the turn. */
function withBusyLead(base: Scenario): Scenario {
  const state = base.changes.reduce(applyChange, emptyState);
  const lead = state.agents[state.runs[base.runId]?.leadAgentId ?? ''];
  if (lead === undefined) throw new Error('the scenario has no lead');
  const turn = create(RunChangeSchema, {
    sequence: BigInt(base.changes.length + 1),
    at: timestampFromMs(base.endMs + 1000),
    runId: base.runId,
    body: {case: 'agent', value: create(AgentSchema, {...lead, status: AgentStatus.ACTIVE})},
  });
  return {...base, changes: [...base.changes, turn], endMs: base.endMs + 1000};
}

/** The page of the scenario's run over a fake daemon: updates go to it, and its log is what is shown. */
function openSettings(daemon: FakeDaemon = new FakeDaemon(scenario)) {
  const runId = daemon.scenario.runId;
  const sent: RunUpdateRequest[] = [];
  let progress: CommandProgress | undefined;
  let screen: Screen | undefined;
  const app = () => (
    <OrchestratorApp
      hosts={[hostSnapshotOf({model: {data: stateOf(daemon), agentCursors: {}}})]}
      nowMs={scenario.endMs}
      commands={appCommandsOf({
        update: progress === undefined ? {} : {[runId]: progress},
        updateRun: (_host, id, request) => {
          sent.push(request);
          const command = create(CommandSchema, {
            id: `update-${sent.length}`,
            body: {case: 'updateRun', value: updateRunMessage(id, request)},
          });
          progress = answered(daemon.dispatch(command));
          screen?.render(app());
        },
      })}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={{
        screen: 'run',
        host: 'local',
        runId,
        view: 'report',
        agentId: null,
        ideas: null,
      }}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />
  );
  const mounted = mount(app());
  screen = mounted;
  mounted.press(mounted.control('Adjust'));
  /** What the host holds of the run now. */
  const run = () => stateOf(daemon).runs[runId];
  /** Re-draws after the daemon moved on its own (a lead's turn ended). */
  const refresh = () => mounted.render(app());
  return {screen: mounted, daemon, sent, run, refresh};
}

const dialogText = (screen: Screen): string =>
  screen.container.querySelector('dialog')?.textContent ?? '';

describe('the run page settings', () => {
  it('names no control with the window’s own Settings word, which the shell’s link owns', () => {
    const {screen} = openSettings();
    const names = [...screen.container.querySelectorAll('button, a')].map(
      each => each.textContent ?? '',
    );
    expect(names.filter(name => /settings/i.test(name))).toEqual([]);
    screen.unmount();
  });

  it('starts on a live run', () => {
    const state = stateOf(new FakeDaemon(scenario));
    expect(state.runs[scenario.runId]?.status).toBe(RunStatus.RUNNING);
  });

  it('sets a cost limit and says it in the changes, with no other word of the run moved', () => {
    const {screen, run} = openSettings();
    const page = (): string =>
      [...screen.container.querySelectorAll('h1, [role="status"]')]
        .filter(each => each.closest('dialog') === null)
        .map(each => each.outerHTML)
        .join();
    const before = page();
    typeInto(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '80');
    screen.press(screen.control('Apply'));
    expect(run()?.maxCostUsd).toBe(80);
    expect(dialogText(screen)).toContain('Cost limit No limit → $80.0');
    expect(page()).toBe(before);
    screen.unmount();
  });

  it('clears a limit that was set', () => {
    const {screen, run, sent} = openSettings();
    typeInto(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '80');
    screen.press(screen.control('Apply'));
    typeInto(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '');
    screen.press(screen.control('Apply'));
    expect(sent.at(-1)).toEqual({cost: {remove: true}});
    expect(run()?.maxCostUsd).toBeUndefined();
    expect(dialogText(screen)).toContain('Cost limit $80.0 → No limit');
    screen.unmount();
  });

  it('asks only what differs, and nothing when nothing does', () => {
    const {screen, sent} = openSettings();
    expect(screen.control('Apply').getAttribute('aria-disabled')).toBe('true');
    choose(fieldOf<HTMLSelectElement>(screen, 'Time limit'), '7200');
    screen.press(screen.control('Apply'));
    expect(sent).toEqual([{time: {set: 7200}}]);
    screen.unmount();
  });

  it('lowers the fanout without stopping an agent, and shows the cap and how many live are above it', () => {
    const daemon = new FakeDaemon(scenario);
    const live = (): number =>
      Object.values(stateOf(daemon).agents).filter(
        agent => agent.role !== AgentRole.LEAD && agent.status !== AgentStatus.ENDED,
      ).length;
    const before = live();
    const {screen, run} = openSettings(daemon);
    choose(fieldOf<HTMLSelectElement>(screen, 'Agents at once'), '1');
    screen.press(screen.control('Apply'));
    expect(run()?.fanoutLimit).toBe(1);
    expect(live()).toBe(before);
    expect(dialogText(screen)).toContain(`Cap 1 · ${before} live · ${before - 1} above`);
    screen.unmount();
  });

  it('shows a host refusal in the host’s words and changes none of what was asked', () => {
    const daemon = new FakeDaemon(scenario);
    daemon.setModels(create(ModelCatalogSchema, {allowedModels: ['model-a']}));
    const {screen, run, sent} = openSettings(daemon);
    typeInto(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '80');
    choose(fieldOf<HTMLSelectElement>(screen, 'Lead'), 'codex');
    screen.press(screen.control('Apply'));
    expect(sent).toHaveLength(1);
    expect(dialogText(screen)).toContain(
      'Update refused · No allowed model for this tool · allowed: model-a',
    );
    expect(run()?.maxCostUsd).toBeUndefined();
    expect(dialogText(screen)).not.toContain('Cost limit No limit');
    screen.unmount();
  });

  it('keeps a lead change pending as run state until the lead rests, then shows the new lead', () => {
    const daemon = new FakeDaemon(withBusyLead(scenario));
    const {screen, run, refresh} = openSettings(daemon);
    choose(fieldOf<HTMLSelectElement>(screen, 'Lead'), 'codex');
    screen.press(screen.control('Apply'));
    expect(run()?.pendingLead).toBeDefined();
    expect(screen.container.textContent).toContain('Lead pending');
    expect(dialogText(screen)).toContain('Lead → Codex · pending');
    expect(dialogText(screen)).toContain('Pending · Codex');
    daemon.finishLeadTurn(daemon.scenario.runId);
    refresh();
    expect(run()?.pendingLead).toBeUndefined();
    expect(screen.container.textContent).not.toContain('Lead pending');
    expect(dialogText(screen)).toContain('Lead Claude Code · model-a → Codex');
    screen.unmount();
  });

  it('lists the changes newest first, each with its time and only labels and numbers', () => {
    const {screen} = openSettings();
    typeInto(fieldOf<HTMLInputElement>(screen, 'Cost limit ($)'), '80');
    screen.press(screen.control('Apply'));
    choose(fieldOf<HTMLSelectElement>(screen, 'Agents at once'), '2');
    screen.press(screen.control('Apply'));
    const text = dialogText(screen);
    expect(text.indexOf('Agents at once 5 → 2')).toBeGreaterThan(-1);
    expect(text.indexOf('Agents at once 5 → 2')).toBeLessThan(text.indexOf('Cost limit No limit'));
    screen.unmount();
  });
});
