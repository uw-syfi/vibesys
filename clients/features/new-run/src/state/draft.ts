/**
 * The unfinished New run a window keeps: what the answers say, the step shown, and the run's own
 * choices (machine, agent, budget, extra arguments, where an example is copied to). It is stored as
 * text, read back through `parseDraft`, which rejects any text that is not exactly a draft, so a
 * stale or hand-edited value is no draft and never half of one.
 */
import {type HostKey, hostFromKey} from '@vibesys/hosts';
import type {MeasureTarget} from '@vibesys/ipc';
import {type Answers, NO_ANSWERS, type StepId} from './answers.js';
import type {CatalogueEntry} from './catalogue.js';
import {type Flow, formFor} from './flow.js';
import type {AgentChoice, AgentPick, Budget, Effort, Form} from './form.js';

/** The key a window's storage keeps its draft under. */
export const DRAFT_KEY = 'vibesys.new-run.draft';

export interface Draft {
  readonly machine: HostKey;
  readonly answers: Answers;
  readonly at: StepId;
  readonly agent: AgentChoice | null;
  readonly budget: Budget;
  readonly measure: MeasureTarget;
  readonly advanced: string;
  /** Where an example's copy goes, when the user changed it. */
  readonly destination: string | null;
}

/** The draft of `flow`, or null while nothing is answered (there is nothing to keep). */
export function draftOf(flow: Flow): Draft | null {
  if (flow.answers.from === null) return null;
  const {form} = flow;
  const project = form.project;
  return {
    machine: form.machine,
    answers: flow.answers,
    at: flow.at,
    agent: form.agent,
    budget: form.budget,
    measure: form.measure,
    advanced: form.advanced,
    destination:
      project !== null && (project.source === 'example' || project.source === 'created')
        ? project.destination
        : null,
  };
}

/** The flow a draft was taken from, its form naming what the answers name now. */
export function flowOfDraft(draft: Draft, entries: readonly CatalogueEntry[]): Flow {
  const base: Form = {
    machine: draft.machine,
    project: null,
    task: null,
    agent: draft.agent,
    budget: draft.budget,
    measure: draft.measure,
    advanced: draft.advanced,
  };
  const form = formFor(base, draft.answers, entries);
  const project = form.project;
  const moved: Form =
    draft.destination !== null && project !== null && project.source === 'example'
      ? ({...form, project: {...project, destination: draft.destination}} satisfies Form)
      : form;
  return {answers: draft.answers, at: draft.at, form: moved};
}

const STEP_IDS: readonly StepId[] = ['pick', 'review'];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** The keys of `value`, only when they are exactly `keys`. */
function exact(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (!isRecord(value)) return null;
  const own = Object.keys(value);
  return own.length === keys.length && keys.every(key => own.includes(key)) ? value : null;
}

const text = (value: unknown): string | null => (typeof value === 'string' ? value : null);
const nullableText = (value: unknown): string | null | undefined =>
  value === null ? null : typeof value === 'string' ? value : undefined;

const EFFORTS: readonly Effort[] = ['default', 'low', 'medium', 'high'];

function parseOwnProject(value: unknown): Answers['project'] | undefined {
  if (value === null) return null;
  const fields = exact(value, ['source', 'path']);
  const path = text(fields?.['path']);
  const source = fields?.['source'];
  if (path === null || (source !== 'recent' && source !== 'folder')) return undefined;
  return {source, path};
}

function parseEntry(value: unknown): Answers['entry'] | undefined {
  if (value === null) return null;
  const fields = exact(value, ['example', 'task']);
  const example = text(fields?.['example']);
  const task = text(fields?.['task']);
  return example === null || task === null ? undefined : {example, task};
}

function parseAnswers(value: unknown): Answers | null {
  const fields = exact(value, Object.keys(NO_ANSWERS));
  if (fields === null) return null;
  const from = fields['from'];
  const entry = parseEntry(fields['entry']);
  const project = parseOwnProject(fields['project']);
  const task = nullableText(fields['task']);
  if (
    (from !== 'example' && from !== 'project') ||
    entry === undefined ||
    project === undefined ||
    task === undefined
  ) {
    return null;
  }
  return {from, entry, project, task};
}

function parsePick(value: unknown): AgentPick | null | undefined {
  if (value === null) return null;
  const fields = exact(value, ['provider', 'model']);
  const provider = text(fields?.['provider']);
  const model = text(fields?.['model']);
  return provider === null || model === null ? undefined : {provider, model};
}

function parseAgent(value: unknown): AgentChoice | null | undefined {
  if (value === null) return null;
  const fields = exact(value, ['provider', 'model', 'roles']);
  const provider = text(fields?.['provider']);
  const model = text(fields?.['model']);
  const roles = fields?.['roles'];
  if (provider === null || model === null || !Array.isArray(roles)) return undefined;
  const parsed = roles.map(role => {
    const each = exact(role, ['role', 'agent', 'effort']);
    const name = text(each?.['role']);
    const roleAgent = parsePick(each?.['agent']);
    const effort = EFFORTS.find(known => known === each?.['effort']);
    return name === null || roleAgent === undefined || effort === undefined
      ? null
      : {role: name, agent: roleAgent, effort};
  });
  return parsed.every(each => each !== null)
    ? {provider, model, roles: parsed as AgentChoice['roles']}
    : undefined;
}

function parseMeasure(value: unknown): MeasureTarget | null {
  const kind = isRecord(value) ? value['kind'] : undefined;
  if (kind === 'same_machine' || kind === 'modal') return exact(value, ['kind']) ? {kind} : null;
  const fields = kind === 'slurm' ? exact(value, ['kind', 'cluster']) : null;
  const cluster = text(fields?.['cluster']);
  return cluster === null ? null : {kind: 'slurm', cluster};
}

function parseBudget(value: unknown): Budget | null {
  const fields = exact(value, ['rounds', 'timeLimitS', 'results']);
  if (fields === null) return null;
  const {rounds, timeLimitS, results} = fields;
  const count = (each: unknown): boolean =>
    each === null || (typeof each === 'number' && Number.isInteger(each) && each > 0);
  if (!count(rounds) || !count(timeLimitS)) return null;
  if (results !== 'keep' && results !== 'publish_github') return null;
  return {rounds: rounds as number | null, timeLimitS: timeLimitS as number | null, results};
}

/** `value` when it names a host, else null. */
function hostKeyOf(value: unknown): HostKey | null {
  try {
    hostFromKey(value);
    return value as HostKey;
  } catch {
    return null;
  }
}

/** `serialized` as a draft, or null when it is not exactly the text `storeDraft` writes. */
export function parseDraft(serialized: string | null): Draft | null {
  if (serialized === null) return null;
  let value: unknown;
  try {
    value = JSON.parse(serialized);
  } catch {
    return null;
  }
  const fields = exact(value, [
    'machine',
    'answers',
    'at',
    'agent',
    'budget',
    'measure',
    'advanced',
    'destination',
  ]);
  if (fields === null) return null;
  const machine = hostKeyOf(fields['machine']);
  const answers = parseAnswers(fields['answers']);
  const at = STEP_IDS.find(step => step === fields['at']);
  const agent = parseAgent(fields['agent']);
  const budget = parseBudget(fields['budget']);
  const measure = parseMeasure(fields['measure']);
  const advanced = text(fields['advanced']);
  const destination = nullableText(fields['destination']);
  if (
    machine === null ||
    answers === null ||
    at === undefined ||
    agent === undefined ||
    budget === null ||
    measure === null ||
    advanced === null ||
    destination === undefined
  ) {
    return null;
  }
  return {machine, answers, at, agent, budget, measure, advanced, destination};
}

export function storeDraft(draft: Draft): string {
  return JSON.stringify(draft);
}
