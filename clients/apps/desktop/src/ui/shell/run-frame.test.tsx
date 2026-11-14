/**
 * Every destination of the window is drawn in one page frame: the pages of a run (its report, its
 * cost, its agents, one agent) and Catch me up are the same `Page` as Runs and Settings, with the
 * same width, a trail band above a real `<h1>`, and none of the pieces the old run screen put
 * around them (a side list of agents, tabs, the cards of the search).
 */
import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {RunSummarySchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {JSX} from 'react';
import {
  applyChange,
  applyCost,
  applySearch,
  emptyState,
  initialAppearance,
  initialNavigation,
  type OrchestratorState,
  type Route,
  type RunView,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {PRICED_AND_UNPRICED} from '../../testing/orchestrator/cost-views.js';
import {generateScenario, measuredSearch, SERVING} from '../../testing/orchestrator/index.js';
import {CatchUpScreen} from '../catch-up/index.js';
import {CostOverviewScreen} from '../cost-overview/index.js';
import {MachinesScreen} from '../machines/index.js';
import {type HostSnapshot, OrchestratorApp} from '../orchestrator/app.js';
import {runTitle} from '../orchestrator/fleet-words.js';
import {faultWord, roleWord} from '../orchestrator/words.js';
import {SettingsScreen} from '../settings/settings-screen.js';
import {COST_OVERVIEW_WORDS} from '../test-display-clock.js';

installDom();

const PIECES_OF_THE_OLD_FRAME =
  /Result so far|Candidates|Task job queue|Plans|Handoffs|Timeline|Views of this run|Agents in this run/;

function hostOf(
  state: OrchestratorState,
  runHistory: HostSnapshot['runHistory'] = {_tag: 'Idle'},
): HostSnapshot {
  return hostSnapshotOf({runHistory, model: {data: state, agentCursors: {}}});
}

function world(seed: number, subagents: number, live: boolean) {
  const scenario = generateScenario({seed, subagents, live, tree: true, depth: 1});
  let state = scenario.changes.reduce(applyChange, emptyState);
  state = applySearch(
    state,
    measuredSearch(scenario.runId, {
      seed,
      candidates: 12,
      startMs: scenario.endMs - 3_600_000,
      endMs: scenario.endMs,
      objectives: SERVING,
    }),
  );
  state = applyCost(state, {...PRICED_AND_UNPRICED, runId: scenario.runId});
  return {scenario, state};
}

function app(
  state: OrchestratorState,
  nowMs: number,
  route: Route,
  runHistory?: HostSnapshot['runHistory'],
): JSX.Element {
  return (
    <OrchestratorApp
      hosts={[hostOf(state, runHistory)]}
      nowMs={nowMs}
      commands={appCommandsOf()}
      retryHost={() => undefined}
      retryCost={() => undefined}
      route={route}
      fleet={initialNavigation.fleet}
      go={() => undefined}
    />
  );
}

/** What the frame of the page on screen is: its width, its trail band and its one heading. */
function frameOf(container: HTMLElement) {
  const pages = container.querySelectorAll('main.vs-page');
  const content = container.querySelector('.vs-page-content');
  const header = container.querySelector('.vs-page-header');
  return {
    pages: pages.length,
    width: content?.getAttribute('data-width'),
    // The heading's row follows the trail band at once; a page with no trail has no band at all.
    titleFirst:
      header !== null &&
      (header.firstElementChild?.classList.contains('vs-page-trail') === true
        ? header.children[1]
        : header.firstElementChild
      )?.querySelector('h1') !== null,
    headings: container.querySelectorAll('h1').length,
    title: container.querySelector('h1')?.textContent ?? '',
  };
}

describe('the pages of a run and the pages of the window', () => {
  it('Runs, Settings, the report, Cost, Agents and one agent share one frame', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 20}), fc.boolean(), (seed, live) => {
        const {scenario, state} = world(seed, 4, live);
        const nowMs = scenario.endMs + 60_000;
        const agent = Object.values(state.agents).find(each => each.parentAgentId !== '');
        const routes: readonly [string, Route][] = [
          ['runs', {screen: 'runs'}],
          ...(['report', 'cost', 'agents'] as const).map((view: RunView): [string, Route] => [
            view,
            {screen: 'run', host: 'local', runId: scenario.runId, view, agentId: null, ideas: null},
          ]),
          [
            'agent',
            {
              screen: 'run',
              host: 'local',
              runId: scenario.runId,
              view: 'agents',
              agentId: agent?.id ?? '',
              ideas: null,
            },
          ],
        ];
        for (const [name, route] of routes) {
          const screen = mount(app(state, nowMs, route));
          const frame = frameOf(screen.container);
          expect(frame, name).toMatchObject({
            pages: 1,
            // The report is a grid of panels in two columns (the main one and a side of 400 px) that
            // stack under 1180 px, which a page of 1100 px never reaches: it takes the window's width.
            // Its trail band, title row and left edge are the frame every other page has.
            width: name === 'report' ? 'fill' : 'wide',
            titleFirst: true,
            headings: 1,
          });
          expect(frame.title, name).not.toBe('');
          // Nothing of the old run screen stands beside or around the page.
          expect(screen.container.querySelector('.vs-screen'), name).toBeNull();
          expect(screen.container.querySelector('[role="tablist"]'), name).toBeNull();
          expect(screen.container.querySelector('[role="tree"]'), name).toBeNull();
          // The list of runs has columns of its own (its candidates); only a run's pages are checked.
          if (name !== 'runs') expect(screen.text(), name).not.toMatch(PIECES_OF_THE_OLD_FRAME);
          screen.unmount();
        }
      }),
      {numRuns: 6},
    );
  });

  it('Settings, Machines, Cost and Catch me up are in the same frame, with the same heading row', () => {
    const settings = mount(
      <SettingsScreen appearance={initialAppearance('light')} choose={() => undefined} />,
    );
    expect(frameOf(settings.container)).toMatchObject({
      pages: 1,
      width: 'wide',
      titleFirst: true,
      headings: 1,
      title: 'Settings',
    });
    settings.unmount();
    // The report and Cost are tables that need the whole area, and fill it; Machines is a card grid,
    // wide like Settings.
    for (const [name, width, page] of [
      ['Machines', 'wide', <MachinesScreen key="m" rows={[]} />],
      [
        'Cost',
        'fill',
        <CostOverviewScreen
          key="c"
          hosts={[]}
          words={COST_OVERVIEW_WORDS}
          open={() => undefined}
          retry={() => undefined}
        />,
      ],
    ] as const) {
      const screen = mount(page);
      expect(frameOf(screen.container), name).toMatchObject({
        pages: 1,
        width,
        titleFirst: true,
        headings: 1,
        title: name,
      });
      screen.unmount();
    }
    const catchUp = mount(
      <CatchUpScreen
        run="a-run"
        rows={[]}
        history={{_tag: 'Idle'}}
        retryHistory={() => undefined}
        press={undefined}
        catchUp={() => undefined}
        goRuns={() => undefined}
        goRun={() => undefined}
        goIdeas={() => undefined}
        words={{role: roleWord, fault: faultWord}}
      />,
    );
    expect(frameOf(catchUp.container)).toMatchObject({
      pages: 1,
      width: 'wide',
      titleFirst: true,
      headings: 1,
      title: 'Catch me up',
    });
    catchUp.unmount();
  });

  it('says where the read of the run’s history stands among the words under the title of every page of it', () => {
    const {scenario, state} = world(3, 3, true);
    const agent = Object.values(state.agents).find(each => each.parentAgentId !== '');
    const routes: Route[] = [
      {
        screen: 'run',
        host: 'local',
        runId: scenario.runId,
        view: 'report',
        agentId: null,
        ideas: null,
      },
      {
        screen: 'run',
        host: 'local',
        runId: scenario.runId,
        view: 'cost',
        agentId: null,
        ideas: null,
      },
      {
        screen: 'run',
        host: 'local',
        runId: scenario.runId,
        view: 'agents',
        agentId: null,
        ideas: null,
      },
      {
        screen: 'run',
        host: 'local',
        runId: scenario.runId,
        view: 'agents',
        agentId: agent?.id ?? '',
        ideas: null,
      },
    ];
    for (const route of routes) {
      const idle = mount(app(state, scenario.endMs, route));
      expect(idle.container.querySelector('.vs-page-meta')?.textContent).not.toContain('history');
      idle.unmount();
      const reading = mount(app(state, scenario.endMs, route, {_tag: 'Loading'}));
      expect(reading.container.querySelector('.vs-page-meta')?.textContent).toContain(
        'Reading history',
      );
      // It is one of the words of the title row, not a banner above the page.
      expect(reading.container.querySelector('.vs-page-body')?.textContent).not.toContain(
        'Reading history',
      );
      reading.unmount();
    }
  });

  it('puts the run in the trail of every page of it, each step but the last a way back', () => {
    const {scenario, state} = world(3, 3, true);
    const run = state.runs[scenario.runId];
    const task = run === undefined ? '' : runTitle(run.taskCard ?? null, run.task);
    for (const view of ['report', 'cost', 'agents'] as const) {
      const screen = mount(
        app(state, scenario.endMs, {
          screen: 'run',
          host: 'local',
          runId: scenario.runId,
          view,
          agentId: null,
          ideas: null,
        }),
      );
      const steps = [
        ...screen.container.querySelectorAll('nav[aria-label="Where you are"] .vs-crumb'),
      ];
      expect(steps.map(step => step.textContent)).toEqual(
        view === 'report' ? ['Runs', task] : ['Runs', task, view === 'cost' ? 'Cost' : 'Agents'],
      );
      expect(steps.slice(0, -1).every(step => step.tagName === 'BUTTON')).toBe(true);
      expect(steps.at(-1)?.getAttribute('aria-current')).toBe('location');
      screen.unmount();
    }
  });

  it('says in the words under the title of every page of a run when the host could not serve its report', () => {
    const {scenario, state} = world(3, 2, true);
    const nowMs = scenario.endMs + 60_000;
    const flagged = (flag: boolean): OrchestratorState => ({
      ...state,
      summaries: {
        ...state.summaries,
        [scenario.runId]: create(RunSummarySchema, {
          runId: scenario.runId,
          reportUnavailable: flag,
        }),
      },
    });
    for (const view of ['report', 'cost', 'agents'] as const) {
      const route: Route = {
        screen: 'run',
        host: 'local',
        runId: scenario.runId,
        view,
        agentId: null,
        ideas: null,
      };
      const on = mount(app(flagged(true), nowMs, route));
      expect(on.container.querySelector('.vs-page-meta')?.textContent, view).toContain(
        'Report unavailable',
      );
      on.unmount();
      const off = mount(app(flagged(false), nowMs, route));
      expect(off.container.textContent, view).not.toContain('Report unavailable');
      off.unmount();
    }
  });
});
