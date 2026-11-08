/**
 * The settings form's fields as typed, and the UpdateRun they become: only what differs from what
 * the run has now is asked, and a form that changes nothing asks nothing.
 */
import type {Harness} from '@vibesys/orchestrator-proto';
import type {LeadSetting, RunSettings} from '../../../state/index.js';
import {LEADS, type LeadChoice} from '../start-draft.js';

/** A limit set to a number or taken off. */
type LimitEdit = {readonly set: number} | {readonly remove: true};

/** What an update asks: each field present is a setting to change, as in the UpdateRun command. */
export interface RunUpdateRequest {
  readonly cost?: LimitEdit;
  /** The time limit's new total, in seconds. */
  readonly time?: LimitEdit;
  readonly fanout?: number;
  readonly lead?: {readonly harness: Harness; readonly model: string};
}

export const NO_LIMIT = 'none';

/** The fields as typed. Cost is dollars, empty for none; time is seconds or `none`; the model is empty for the tool's own. */
export interface SettingsDraft {
  readonly cost: string;
  readonly time: string;
  readonly fanout: string;
  readonly lead: LeadChoice;
  readonly model: string;
}

const choiceOf = (harness: Harness): LeadChoice =>
  (Object.entries(LEADS).find(([, value]) => value === harness)?.[0] ??
    'claude_code') as LeadChoice;

/** The lead the form starts from: the one waiting to take over, else the one running. */
const leadOfSettings = (settings: RunSettings): LeadSetting | null =>
  settings.lead.pending ?? settings.lead.current;

/** The form as it opens: every field at what the run has now. */
export function draftOf(settings: RunSettings): SettingsDraft {
  const lead = leadOfSettings(settings);
  return {
    cost: settings.cost.limitUsd === null ? '' : String(settings.cost.limitUsd),
    time: settings.time.limitSeconds === null ? NO_LIMIT : String(settings.time.limitSeconds),
    fanout: String(settings.fanout.cap),
    lead: lead === null ? 'claude_code' : choiceOf(lead.harness),
    model: lead?.model ?? '',
  };
}

/** The cost as a number, null for none and NaN for text that is not a positive amount. */
function costOf(typed: string): number | null {
  const text = typed.trim();
  if (text === '') return null;
  const amount = Number(text);
  return Number.isFinite(amount) && amount > 0 ? amount : Number.NaN;
}

/** What the form asks of the run, or why it cannot be asked yet. */
export type Asked =
  | {readonly kind: 'nothing'}
  | {readonly kind: 'invalid'; readonly field: 'cost'}
  | {readonly kind: 'update'; readonly request: RunUpdateRequest};

function costEdit(draft: SettingsDraft, settings: RunSettings): LimitEdit | null | 'invalid' {
  const typed = costOf(draft.cost);
  if (Number.isNaN(typed)) return 'invalid';
  if (typed === settings.cost.limitUsd) return null;
  return typed === null ? {remove: true} : {set: typed};
}

function timeEdit(draft: SettingsDraft, settings: RunSettings): LimitEdit | null {
  const seconds = draft.time === NO_LIMIT ? null : Number(draft.time);
  if (seconds === settings.time.limitSeconds) return null;
  return seconds === null ? {remove: true} : {set: seconds};
}

function leadEdit(
  draft: SettingsDraft,
  settings: RunSettings,
): NonNullable<RunUpdateRequest['lead']> | null {
  const held = leadOfSettings(settings);
  if (held === null) return null;
  const harness = LEADS[draft.lead];
  return harness === held.harness && draft.model === held.model
    ? null
    : {harness, model: draft.model};
}

/** The update the draft asks, carrying only the settings that differ from the run's. */
export function asked(draft: SettingsDraft, settings: RunSettings): Asked {
  const cost = costEdit(draft, settings);
  if (cost === 'invalid') return {kind: 'invalid', field: 'cost'};
  const time = timeEdit(draft, settings);
  const fanout = Number(draft.fanout);
  const lead = leadEdit(draft, settings);
  const request: RunUpdateRequest = {
    ...(cost === null ? {} : {cost}),
    ...(time === null ? {} : {time}),
    ...(fanout === settings.fanout.cap ? {} : {fanout}),
    ...(lead === null ? {} : {lead}),
  };
  return Object.keys(request).length === 0 ? {kind: 'nothing'} : {kind: 'update', request};
}
