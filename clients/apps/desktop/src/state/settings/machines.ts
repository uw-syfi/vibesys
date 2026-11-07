/** The Add machine input as a fold: what was typed, the aliases offered for it, and why an add was refused. */

export interface AddMachineState {
  readonly input: string;
  /** The aliases offered for `input`. */
  readonly matches: readonly string[];
  /** Why the last add was refused; cleared by the next edit. */
  readonly error: string | null;
  /** An add is under way: the input is held until it ends. */
  readonly busy: boolean;
}

export const emptyAddMachine: AddMachineState = {input: '', matches: [], error: null, busy: false};

export type AddMachineEvent =
  | {readonly type: 'typed'; readonly input: string}
  | {readonly type: 'matched'; readonly query: string; readonly aliases: readonly string[]}
  | {readonly type: 'submitted'}
  | {readonly type: 'refused'; readonly reason: string};

export function addMachineStep(state: AddMachineState, event: AddMachineEvent): AddMachineState {
  switch (event.type) {
    case 'typed':
      return state.busy ? state : {...state, input: event.input, error: null};
    case 'matched':
      // An answer for an earlier edit is stale: only the one for what is typed now is offered.
      return event.query === state.input ? {...state, matches: event.aliases} : state;
    case 'submitted':
      return {...state, busy: true, error: null};
    case 'refused':
      return {...state, busy: false, error: event.reason};
  }
}
