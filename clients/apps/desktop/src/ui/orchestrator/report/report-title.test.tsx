/**
 * The report's title row through the DOM, over a search state the host serves as unreadable: what
 * the chip of the search says for each reason the protocol names, with the host's own words and the
 * time it gave, or without one.
 */

import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  RunStatus,
  RunStopReason,
  RunStopReasonSchema,
  SearchStateSchema,
  UnreadableReason,
  UnreadableReasonSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyChange, applySearch, emptyState, runReport} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {generateScenario} from '../../../testing/orchestrator/index.js';
import {causeWord} from '../../refusal-words.js';
import {formatAge} from './format.js';
import {ReportMeta} from './report-title.js';

installDom();

const scenario = generateScenario({seed: 4, subagents: 2, live: true});
const RUN = scenario.runId;
const NOW = scenario.endMs + 10 * 60_000;
const held = scenario.changes.reduce(applyChange, emptyState);

/** Every reason the protocol names, the unspecified one too. */
const REASONS = UnreadableReasonSchema.values.map(value => value.number as UnreadableReason);

interface Unreadable {
  readonly reason: UnreadableReason;
  readonly detail: string;
  /** When the host says it found the job log unreadable, or undefined when it says no time. */
  readonly sinceMs: number | undefined;
}

/** The chip of the search in the title row, as a person reads it: its label, tone and hint. */
function searchChip(unreadable: Unreadable) {
  const search = create(SearchStateSchema, {
    runId: RUN,
    revision: 1n,
    health: {
      state: {
        case: 'unreadable',
        value: {
          reason: unreadable.reason,
          detail: unreadable.detail,
          ...(unreadable.sinceMs === undefined ? {} : {since: timestampFromMs(unreadable.sinceMs)}),
        },
      },
    },
  });
  const report = runReport(applySearch(held, search), RUN, NOW);
  if (report === undefined) throw new Error('the scenario has its run');
  const screen = mount(<ReportMeta report={report} host="host-a" nowMs={NOW} />);
  // With the host live, the only chip with an account behind it is the search's.
  const chips = [...screen.container.querySelectorAll('.vs-chip[aria-description]')];
  expect(chips).toHaveLength(1);
  const [chip] = chips;
  const read = {
    label: chip?.textContent ?? '',
    tone: chip?.getAttribute('data-tone') ?? '',
    hint: chip?.getAttribute('aria-description') ?? '',
  };
  screen.unmount();
  return read;
}

/** Words the host may send as its detail; no path, which a chip shows by its last name only. */
const detail = fc.stringMatching(/^[A-Za-z0-9 ,.:;()'=-]{0,80}$/);

describe('the chip of a search the host cannot read', () => {
  it('names each reason in a label and an account of its own', () => {
    const chips = REASONS.map(reason => searchChip({reason, detail: '', sinceMs: NOW - 60_000}));
    expect(new Set(chips.map(chip => chip.label)).size).toBe(REASONS.length);
    expect(new Set(chips.map(chip => chip.hint)).size).toBe(REASONS.length);
  });

  it('calls a first read still in flight not read yet, and every other reason a warning', () => {
    for (const reason of REASONS) {
      const chip = searchChip({reason, detail: '', sinceMs: NOW - 60_000});
      const loading = reason === UnreadableReason.NO_DAEMON;
      expect(chip.tone).toBe(loading ? 'neutral' : 'warn');
      // Before the first read there is nothing measured to be out of date.
      expect(chip.hint.includes('measured before')).toBe(!loading);
    }
  });

  it('calls a missing job tool what a host that refuses for it is called', () => {
    const chip = searchChip({
      reason: UnreadableReason.CLI_NOT_INSTALLED,
      detail: '',
      sinceMs: NOW - 60_000,
    });
    expect(chip.label).toBe(causeWord('job-tool-missing'));
  });

  it('says the host’s own words, and since when or that the host said no time', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...REASONS),
        detail,
        fc.option(fc.integer({min: 0, max: NOW}), {nil: undefined}),
        (reason, words, sinceMs) => {
          const {hint} = searchChip({reason, detail: words, sinceMs});
          const details = words === '' ? '' : ` Details: ${words}`;
          expect(hint.endsWith(details)).toBe(true);
          // What the app says, before the host's own words.
          const account = hint.slice(0, hint.length - details.length);
          expect(account).not.toContain('Details:');
          if (sinceMs === undefined) {
            expect(account).toContain('The host did not say since when.');
            expect(account).not.toContain(' ago');
          } else {
            expect(account).toContain(formatAge(NOW - sinceMs));
            expect(account).not.toContain('did not say since when');
          }
        },
      ),
      {numRuns: 60},
    );
  });
});

describe('the state of the run in the title row', () => {
  const metaOf = (status: RunStatus, stopReason: RunStopReason): HTMLElement => {
    const run = held.runs[RUN];
    if (run === undefined) throw new Error('the scenario has its run');
    const state = {...held, runs: {...held.runs, [RUN]: {...run, status, stopReason}}};
    const report = runReport(state, RUN, NOW);
    if (report === undefined) throw new Error('the scenario has its run');
    const screen = mount(<ReportMeta report={report} host="host-a" nowMs={NOW} />);
    return screen.container;
  };

  it('says why a run stopped once, in words, beside its state, and says nothing for a run that finished its work', () => {
    const failed = metaOf(RunStatus.FAILED, RunStopReason.LEAD_GONE);
    expect(failed.textContent).toContain('Failed');
    expect(failed.textContent?.match(/Lead agent lost/g)).toHaveLength(1);
    const finished = metaOf(RunStatus.ENDED, RunStopReason.FINISHED);
    expect(finished.textContent).toContain('Finished');
    expect(finished.textContent).not.toMatch(/Stopped|limit|lost/);
  });

  it('names no stop reason as the protocol spells it, whatever the host recorded', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...RunStopReasonSchema.values.map(each => each.number as RunStopReason)),
        reason => {
          const meta = metaOf(RunStatus.FAILED, reason);
          expect(meta.textContent).not.toMatch(/UNSPECIFIED|STOP_REASON|LEAD_GONE|TIME_LIMIT/);
        },
      ),
    );
  });

  it('says nothing of the host’s link: the connection status beside it is the one place that does', () => {
    const meta = metaOf(RunStatus.RUNNING, RunStopReason.UNSPECIFIED);
    expect(meta.textContent).not.toMatch(
      /Stale|Catching up|Unavailable|Connecting|Waiting|Unreachable/,
    );
  });
});
