/**
 * The catch-up page by what a person reads and does, over check-ins the fake host stored and the
 * app folded: a live run whose lead is writing and then has written, a run that is over, a window
 * the host could not read, the first press of a run, and the press itself going wrong.
 */
import {describe, expect, it} from '@effect/vitest';
import {AgentRole, type Report, RunStatus} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import type {JSX} from 'react';
import {
  type CatchUpPress,
  type CheckInRead,
  type HostModel,
  messageIs,
  rewind,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {windowFactsOf} from '../../testing/orchestrator/check-ins.js';
import {
  busyFacts,
  FAKE_VIEWER,
  FakeDaemon,
  generateScenario,
  hostHarness,
} from '../../testing/orchestrator/index.js';
import {faultWord, roleWord} from '../orchestrator/words.js';
import {CatchUpScreen} from './catch-up-screen.js';

installDom();

const live = generateScenario({seed: 11, subagents: 2, live: true});
const ended = generateScenario({seed: 11, subagents: 2, live: false});
const WORDS = {role: roleWord, fault: faultWord};
const NO_PRESS: CatchUpPress | undefined = undefined;

interface Props {
  readonly rows: readonly CheckInRead[];
  readonly press?: CatchUpPress | undefined;
  readonly catchUp?: () => void;
}

const page = ({rows, press = NO_PRESS, catchUp = () => undefined}: Props): JSX.Element => (
  <CatchUpScreen
    run="faster-decode"
    rows={rows}
    press={press}
    catchUp={catchUp}
    goRuns={() => undefined}
    goRun={() => undefined}
    words={WORDS}
  />
);

/** A host session over `daemon` watching its run stream, after `act` made check-ins on it. */
const SEED = 20261107;

const afterwards = (
  daemon: FakeDaemon,
  runId: string,
  act: (daemon: FakeDaemon, press: () => Effect.Effect<string>) => Effect.Effect<void>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      const h = yield* hostHarness(daemon, {live: true});
      h.mount(h.session.runs.watch);
      yield* h.elapse(60, 5);
      const press = () =>
        Effect.gen(function* () {
          yield* h.session.catchUp.press(runId);
          yield* h.elapse(60, 5);
          const standing = yield* h.read(h.session.catchUp.standing(runId));
          if (standing?._tag !== 'Made') throw new Error('the press was not made');
          return standing.checkInId;
        });
      yield* act(daemon, press);
      yield* h.elapse(60, 5);
      const model: HostModel = yield* h.model;
      return rewind(model.data, runId, FAKE_VIEWER.id);
    }),
  ).pipe(Random.withSeed(SEED));

describe('the catch-up page of a live run', () => {
  it.effect('shows Writing… until the lead answers, with the figures the host froze', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (daemon, press) =>
        Effect.gen(function* () {
          const id = yield* press();
          daemon.settleCheckIn(id, busyFacts(0));
        }),
      );
      const screen = mount(page({rows}));
      const text = screen.text();
      expect(text).toContain('Writing…');
      expect(text).toContain('Since ');
      // The tiles say what the host counted, never a computed figure.
      for (const word of ['Best', 'throughput', '1.84', '2.31', 'p99 latency', '118']) {
        expect(text).toContain(word);
      }
      expect(text).toMatch(/Created\s*2/);
      expect(text).toMatch(/Changed\s*1/);
      expect(text).toMatch(/Started\s*4/);
      expect(text).toMatch(/Ended\s*3/);
      expect(text).toMatch(/Measured\s*7/);
      expect(text).toContain('$13.15');
      expect(text).toContain('2 unpriced');
      // A type with no priced turn says so; it is not free.
      expect(text).toContain(roleWord(AgentRole.JUDGE));
      expect(text).toContain('No price');
      screen.unmount();
    }),
  );

  it.effect('reads the window before the list of check-ins it came from, at any width', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (daemon, press) =>
        Effect.gen(function* () {
          const id = yield* press();
          daemon.settleCheckIn(id, busyFacts(0));
        }),
      );
      const screen = mount(page({rows}));
      const split = screen.container.querySelector('.vs-split');
      // The window is first in the page's order, so a narrow window stacks it above the list; side
      // by side the list is still at the left, as it was.
      expect(split?.firstElementChild?.classList.contains('vs-split-main')).toBe(true);
      expect(split?.lastElementChild?.classList.contains('vs-split-aside')).toBe(true);
      expect(split?.hasAttribute('data-aside-at-start')).toBe(true);
      screen.unmount();
    }),
  );

  it('starts a new check-in from a button that says so, led by the page and its title', () => {
    const screen = mount(page({rows: []}));
    expect(screen.container.querySelector('h1')?.textContent).toBe('Catch me up');
    const buttons = [...screen.container.querySelectorAll('.vs-page-actions .vs-button')];
    expect(buttons.map(button => button.textContent)).toEqual(['New check-in']);
    expect(buttons[0]?.getAttribute('data-variant')).toBe('primary');
    screen.unmount();
  });

  it.effect('shows the lead summary as Markdown, and no Writing…, once it is linked', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (daemon, press) =>
        Effect.gen(function* () {
          const id = yield* press();
          daemon.settleCheckIn(id, busyFacts(0));
          daemon.postReport(id, 'Best moved from 1.84x to 2.31x', 'Kept **fused dispatch**');
        }),
      );
      const screen = mount(page({rows}));
      expect(screen.text()).not.toContain('Writing…');
      expect(screen.text()).toContain('Best moved from 1.84x to 2.31x');
      expect(screen.container.querySelector('strong')?.textContent).toBe('fused dispatch');
      screen.unmount();
    }),
  );

  it.effect('says Working… and draws no figures while the host works them out', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (_daemon, press) =>
        Effect.asVoid(press()),
      );
      const text = mount(page({rows})).text();
      expect(text).toContain('Working…');
      expect(text).not.toContain('Spend');
      expect(text).not.toContain('Started');
    }),
  );

  it.effect('shows the reason inline when the host could not read the window, and no figures', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (daemon, press) =>
        Effect.gen(function* () {
          daemon.failCheckIn(yield* press(), 'the job log is not answering');
        }),
      );
      const screen = mount(page({rows}));
      expect(screen.text()).toContain('Failed');
      expect(screen.text()).toContain('the job log is not answering');
      expect(screen.text()).not.toContain('Started');
      // The page is no banner: nothing is drawn as an alert region.
      expect(screen.container.querySelector('[role="alert"]')).toBeNull();
      screen.unmount();
    }),
  );
});

describe('the catch-up page of a run that is over', () => {
  it.effect('has no Writing…, and shows the reports posted inside the window', () =>
    Effect.gen(function* () {
      const daemon = new FakeDaemon(ended);
      const state = daemon.snapshot();
      const report = state.messages.find(message => messageIs(message, 'report'));
      if (report === undefined) throw new Error('the scenario posts a report');
      const headline = (report.body.value as Report).headline;
      const rows = yield* afterwards(daemon, ended.runId, (host, press) =>
        Effect.gen(function* () {
          host.settleCheckIn(yield* press(), busyFacts(0, [report.id]));
        }),
      );
      expect(state.runs.find(run => run.id === ended.runId)?.status).not.toBe(RunStatus.RUNNING);
      const text = mount(page({rows})).text();
      expect(text).not.toContain('Writing…');
      expect(text).toContain(headline);
    }),
  );
});

describe('the catch-up page with no check-in', () => {
  it('says there is none, and still offers a new check-in', () => {
    let pressed = 0;
    const screen = mount(
      page({
        rows: [],
        catchUp: () => {
          pressed += 1;
        },
      }),
    );
    expect(screen.text()).toContain('No check-ins yet');
    screen.press(screen.control('New check-in'));
    expect(pressed).toBe(1);
    screen.unmount();
  });
});

describe('the press of Catch me up on the page', () => {
  const press = (value: CatchUpPress, rows: readonly CheckInRead[] = []) =>
    mount(page({rows, press: value}));

  it('is disabled while it is on its way', () => {
    const screen = press({_tag: 'Pressing'});
    expect(screen.text()).toContain('Sending');
    expect(screen.control('New check-in').getAttribute('aria-disabled')).toBe('true');
    screen.unmount();
  });

  it('names each refusal in a word, as a chip and never a banner', () => {
    const words = {
      'run-not-found': 'Run not found',
      'no-viewer': 'No viewer',
      'not-permitted': 'Not permitted',
      unexpected: 'Refused',
    } as const;
    for (const [reason, word] of Object.entries(words)) {
      const screen = press({_tag: 'Refused', reason: reason as keyof typeof words});
      expect(screen.text()).toContain(word);
      expect(screen.container.querySelector('[role="alert"]')).toBeNull();
      screen.unmount();
    }
  });

  it('offers Try again when it could not be confirmed, and that press goes to the same call', () => {
    let again = 0;
    const screen = mount(
      page({
        rows: [],
        press: {
          _tag: 'Unconfirmed',
          fault: {kind: 'DeadlineExceeded', detail: 'no reply in 15 s', retryable: true},
        },
        catchUp: () => {
          again += 1;
        },
      }),
    );
    expect(screen.text()).toContain('Not confirmed');
    screen.press(screen.control('Try again'));
    expect(again).toBe(1);
    screen.unmount();
  });

  it('says it was not sent, with what failed', () => {
    const screen = press({
      _tag: 'Failed',
      fault: {kind: 'Unavailable', detail: 'host-a is away', retryable: true},
    });
    expect(screen.text()).toContain('Not sent');
    expect(screen.text()).toContain(faultWord('Unavailable'));
    screen.unmount();
  });
});

describe('the check-ins of the viewer', () => {
  it.effect('are listed with each window’s cost, and picking one shows its frozen facts', () =>
    Effect.gen(function* () {
      const rows = yield* afterwards(new FakeDaemon(live), live.runId, (daemon, press) =>
        Effect.gen(function* () {
          const first = yield* press();
          daemon.settleCheckIn(first, busyFacts(0));
          const second = yield* press();
          // The second window spent nothing the host could price.
          daemon.settleCheckIn(second, windowFactsOf({agentsStarted: 9}));
        }),
      );
      const screen = mount(page({rows}));
      const options = [...screen.container.querySelectorAll<HTMLElement>('[role="option"]')];
      expect(options).toHaveLength(2);
      // The newest is shown first; the older one costs what the host froze in it.
      expect(options[1]?.textContent).toContain('$13.15');
      expect(screen.text()).toMatch(/Started\s*9/);
      const older = options[1];
      if (older === undefined) throw new Error('the older check-in is listed');
      screen.press(older);
      expect(screen.text()).toMatch(/Started\s*4/);
      expect(screen.text()).not.toMatch(/Started\s*9/);
      screen.unmount();
    }),
  );
});
