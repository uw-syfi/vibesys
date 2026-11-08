/** What a run's settings and their changes are called on screen: labels and numbers, no sentences. */
import type {LeadSetting, RunSettings, SettingChangeView} from '../../../state/index.js';
import {span} from '../../duration.js';
import {formatDollars} from '../report/format.js';
import {harnessWord} from '../words.js';

export const NO_LIMIT_WORD = 'No limit';

const costWord = (usd: number | null): string =>
  usd === null ? NO_LIMIT_WORD : formatDollars(usd);

export const timeWord = (seconds: number | null): string =>
  seconds === null ? NO_LIMIT_WORD : span(seconds * 1000);

/** The lead as `Claude Code · model`; no model is the tool's own, so the tool alone. */
export const leadWord = (lead: LeadSetting | null): string =>
  lead === null
    ? 'Unknown'
    : `${harnessWord(lead.harness)}${lead.model === '' ? '' : ` · ${lead.model}`}`;

/** What one setting change says: its label, from and to, and for a lead change whether it waits. */
export function changeWord(change: SettingChangeView): string {
  switch (change.kind) {
    case 'cost':
      return `Cost limit ${costWord(change.from)} → ${costWord(change.to)}`;
    case 'time':
      return `Time limit ${timeWord(change.from)} → ${timeWord(change.to)}`;
    case 'fanout':
      return `Agents at once ${change.from} → ${change.to}`;
    case 'lead':
      return change.state === 'pending'
        ? `Lead → ${leadWord(change.to)} · pending`
        : `Lead ${change.from === null ? '' : `${leadWord(change.from)} `}→ ${leadWord(change.to)}`;
  }
}

/** The line under the cost field: what the run spent against the limit, and its setup apart. */
export function costLine(settings: RunSettings): string {
  const {spentUsd, setupUsd} = settings.cost;
  return [
    spentUsd === null ? null : `Spent ${formatDollars(spentUsd)}`,
    setupUsd === null ? null : `Setup ${formatDollars(setupUsd)}`,
  ]
    .filter(part => part !== null)
    .join(' · ');
}

/** The line under the time field: how long the run has gone against the limit, and its setup apart. */
export function timeLine(settings: RunSettings): string {
  const {elapsedSeconds, setupSeconds} = settings.time;
  return [
    elapsedSeconds === null ? null : `Elapsed ${span(elapsedSeconds * 1000)}`,
    setupSeconds === null ? null : `Setup ${span(setupSeconds * 1000)}`,
  ]
    .filter(part => part !== null)
    .join(' · ');
}

/** The line under the fanout field: the cap, the live agents and how many are above the cap. */
export function fanoutLine(settings: RunSettings): string {
  const {cap, live, above} = settings.fanout;
  return [`Cap ${cap}`, live === null ? null : `${live} live`, above > 0 ? `${above} above` : null]
    .filter(part => part !== null)
    .join(' · ');
}

/** The line under the lead field: the lead waiting to take over, as run state; none when none waits. */
export function leadLine(settings: RunSettings): string | undefined {
  const {pending} = settings.lead;
  return pending === null ? undefined : `Pending · ${leadWord(pending)}`;
}
