/**
 * What a steer is, as the card edits it and as the run protocol takes it: a draft (text, target,
 * mode), the two requests a card sends (`SteerRequest`, `WithdrawRequest`), and the words each
 * choice is labelled with. The wire types come from the contract; nothing here is a second copy.
 */
import {type RequestInput, roleName} from '@vibesys/contracts';

type ControlRequest = Extract<RequestInput, {readonly type: 'command.control'}>['control'];
export type SteerRequest = Extract<ControlRequest, {readonly action: 'steer'}>;
export type WithdrawRequest = Extract<ControlRequest, {readonly action: 'withdraw'}>;
export type SteerTargetKind = NonNullable<NonNullable<SteerRequest['target']>['kind']>;
export type SteerMode = NonNullable<SteerRequest['mode']>;

export interface SteerTarget {
  readonly kind: SteerTargetKind;
  /** The role, attempt or item id; null for the whole run. */
  readonly id: string | null;
}

/** The fields of a steer the user can still change. */
export interface SteerDraft {
  readonly text: string;
  readonly target: SteerTarget;
  readonly mode: SteerMode;
}

export const STEER_MODES: readonly {readonly value: SteerMode; readonly label: string}[] = [
  {value: 'next_turn', label: 'Next turn'},
  {value: 'standing', label: 'Every turn'},
  {value: 'interrupt', label: 'Now'},
];

/** The label of a mode, as the card's When control and its summary say it. */
export function modeLabel(mode: SteerMode): string {
  return STEER_MODES.find(entry => entry.value === mode)?.label ?? mode;
}

/** What a target is called on the card: the run, a role by its display name, an attempt or an item. */
export function targetName(target: SteerTarget): string {
  switch (target.kind) {
    case 'run':
      return 'Whole run';
    case 'role':
      return roleName(target.id ?? '');
    case 'attempt':
      return `Attempt ${target.id ?? ''}`.trim();
    case 'item':
      return `Item ${target.id ?? ''}`.trim();
  }
}

/** A target as one string, the value of the card's single target select. */
export function targetValue(target: SteerTarget): string {
  return target.kind === 'run' ? 'run' : `${target.kind}:${target.id ?? ''}`;
}

/** The target `targetValue` wrote; anything that is not a known kind is the whole run. */
export function parseTargetValue(value: string): SteerTarget {
  const split = value.indexOf(':');
  const kind = split < 0 ? value : value.slice(0, split);
  const id = split < 0 ? '' : value.slice(split + 1);
  if (kind === 'role' || kind === 'attempt' || kind === 'item') return {kind, id};
  return {kind: 'run', id: null};
}

/** The run's roles in order: the ones its start advertised, then any seen since, each once. */
export function runRoles(
  advertised: readonly string[] | null,
  seen: readonly string[],
): readonly string[] {
  return [...new Set([...(advertised ?? []), ...seen])];
}

export interface TargetOption {
  readonly value: string;
  readonly label: string;
}

/**
 * The targets a steer can go to: the whole run, then each of the run's roles, in the run's order.
 * The draft's own target is listed too when it is not one of those (a proposal for an attempt or
 * an item), so a select always shows what the card will send.
 */
export function targetOptions(
  roles: readonly string[],
  current: SteerTarget,
): readonly TargetOption[] {
  const targets: SteerTarget[] = [
    {kind: 'run', id: null},
    ...roles.map((id): SteerTarget => ({kind: 'role', id})),
  ];
  if (!targets.some(target => targetValue(target) === targetValue(current))) targets.push(current);
  return targets.map(target => ({value: targetValue(target), label: targetName(target)}));
}

/** The whole run, next turn: where a steer the user writes starts. */
export const EMPTY_DRAFT: SteerDraft = {
  text: '',
  target: {kind: 'run', id: null},
  mode: 'next_turn',
};

/** Why the draft cannot be sent, as a short word for the Send button; null when it can. */
export function draftProblem(draft: SteerDraft): string | null {
  if (draft.text.trim() === '') return 'Empty';
  if (draft.target.kind !== 'run' && (draft.target.id ?? '').trim() === '') return 'No target';
  return null;
}

/** The `command.control` steer for `draft`, under a control id the shell minted. */
export function steerRequest(controlId: string, draft: SteerDraft): SteerRequest {
  const {target} = draft;
  return {
    control_id: controlId,
    action: 'steer',
    text: draft.text.trim(),
    target:
      target.kind === 'run' ? {kind: 'run'} : {kind: target.kind, id: (target.id ?? '').trim()},
    mode: draft.mode,
  };
}

/** The `command.control` withdrawal of the standing steer sent under `withdraws`. */
export function withdrawRequest(controlId: string, withdraws: string): WithdrawRequest {
  return {control_id: controlId, action: 'withdraw', withdraws};
}
