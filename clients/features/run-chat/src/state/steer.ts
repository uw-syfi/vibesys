/**
 * What a steer is, as the card edits it and as the run protocol takes it: a draft (text, target,
 * mode), the two requests a card sends (`SteerRequest`, `WithdrawRequest`), and the words each
 * choice is labelled with. The wire types come from the contract; nothing here is a second copy.
 */
import type {RequestInput} from '@vibesys/contracts';

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

export const TARGET_KINDS: readonly {readonly value: SteerTargetKind; readonly label: string}[] = [
  {value: 'run', label: 'Run'},
  {value: 'role', label: 'Role'},
  {value: 'attempt', label: 'Attempt'},
  {value: 'item', label: 'Item'},
];

export const STEER_MODES: readonly {readonly value: SteerMode; readonly label: string}[] = [
  {value: 'next_turn', label: 'Next turn'},
  {value: 'standing', label: 'Every turn'},
  {value: 'interrupt', label: 'Now'},
];

/** The label of a mode, as the card's When control and its summary say it. */
export function modeLabel(mode: SteerMode): string {
  return STEER_MODES.find(entry => entry.value === mode)?.label ?? mode;
}

/** The label of a target kind. */
export function targetLabel(kind: SteerTargetKind): string {
  return TARGET_KINDS.find(entry => entry.value === kind)?.label ?? kind;
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
