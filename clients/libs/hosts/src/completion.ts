/**
 * Path completion for a host path field (a checkout, a project): as the user types a path on a host, the
 * directory it names is listed once (`paths.complete`), and the names in it are filtered here, on
 * every keystroke, without asking again. A new directory segment asks for that directory; the
 * shell waits a short pause (`COMPLETION_PAUSE_MS`) before asking, and a reply whose token is not
 * the awaited one is dropped, so a slow listing of a directory the user already left cannot
 * replace the current one.
 */

import {only, type Step} from '@vibesys/store';
import type {HostKey} from './host.js';

/** What directory completion established: one listing, or that this helper does not support it. */
export type PathCompletion =
  | {
      readonly kind: 'listed';
      /** The directory that was listed. */
      readonly directory: string;
      readonly entries: readonly {
        readonly name: string;
        readonly path: string;
        readonly checkout: boolean;
        readonly project: boolean;
      }[];
      readonly truncated: boolean;
    }
  | {readonly kind: 'unsupported'};

/** How a listing request ended: its completion, or a failure (which offers nothing). */
export type CompletionResult =
  | {readonly ok: true; readonly value: PathCompletion}
  | {readonly ok: false; readonly error: string};

type ListedCompletion = Extract<PathCompletion, {readonly kind: 'listed'}>;

/** How long typing must pause before a directory is listed. */
const COMPLETION_PAUSE_MS = 80;
/** The most suggestions a field shows. */
const SHOWN = 50;

export type CompletionField = 'checkout' | 'project';

interface FieldCompletion {
  readonly host: HostKey;
  /** The directory the typed path names (up to its last `/`), or null when it names none. */
  readonly directory: string | null;
  /** The listing awaited for `directory`, or null. */
  readonly awaiting: number | null;
  /** The last listing of `directory`, or null. */
  readonly listed: ListedCompletion | null;
  /** The name typed after the directory. */
  readonly typed: string;
}

export interface CompletionState {
  readonly next: number;
  readonly fields: Readonly<Partial<Record<CompletionField, FieldCompletion>>>;
}

export type CompletionAction =
  | {
      readonly type: 'input';
      readonly field: CompletionField;
      readonly host: HostKey;
      readonly value: string;
    }
  | {
      readonly type: 'reply';
      readonly field: CompletionField;
      readonly token: number;
      readonly result: CompletionResult;
    };

export type CompletionRequest = {
  readonly kind: 'list';
  readonly token: number;
  readonly field: CompletionField;
  readonly host: HostKey;
  readonly directory: string;
  /** Ask after this pause, unless a newer request for the field replaces this one. */
  readonly afterMs: number;
};

export const COMPLETION_EMPTY: CompletionState = {next: 1, fields: {}};

/** The directory a typed path names, and the name typed after it. */
export function splitPath(value: string): {directory: string | null; typed: string} {
  const slash = value.lastIndexOf('/');
  if (slash < 0) return {directory: null, typed: value};
  const directory = value.slice(0, slash + 1);
  const absolute = directory.startsWith('/') || directory.startsWith('~/');
  return {directory: absolute ? directory : null, typed: value.slice(slash + 1)};
}

/** The paths to offer for `field`: its directory's entries whose names start with what was typed. */
export function suggestions(state: CompletionState, field: CompletionField): string[] {
  const completion = state.fields[field];
  if (completion?.listed == null) return [];
  return completion.listed.entries
    .filter(entry => entry.name.startsWith(completion.typed))
    .slice(0, SHOWN)
    .map(entry => `${entry.path}/`);
}

function input(
  state: CompletionState,
  action: Extract<CompletionAction, {type: 'input'}>,
): Step<CompletionState, CompletionRequest> {
  const {field, host, value} = action;
  const {directory, typed} = splitPath(value.trimStart());
  const current = state.fields[field];
  const sameDirectory = current?.host === host && current.directory === directory;
  if (sameDirectory)
    return only({...state, fields: {...state.fields, [field]: {...current, typed}}});
  if (directory === null) {
    const cleared = {host, directory, awaiting: null, listed: null, typed};
    return only({...state, fields: {...state.fields, [field]: cleared}});
  }
  const token = state.next;
  const asked: FieldCompletion = {host, directory, awaiting: token, listed: null, typed};
  return {
    state: {next: token + 1, fields: {...state.fields, [field]: asked}},
    requests: [{kind: 'list', token, field, host, directory, afterMs: COMPLETION_PAUSE_MS}],
  };
}

export function completionStep(
  state: CompletionState,
  action: CompletionAction,
): Step<CompletionState, CompletionRequest> {
  switch (action.type) {
    case 'input':
      return input(state, action);
    case 'reply': {
      const current = state.fields[action.field];
      if (current === undefined || current.awaiting !== action.token) return only(state);
      // A directory that cannot be listed offers nothing; the field itself still takes the path.
      const listed =
        action.result.ok && action.result.value.kind === 'listed' ? action.result.value : null;
      return only({
        ...state,
        fields: {...state.fields, [action.field]: {...current, awaiting: null, listed}},
      });
    }
  }
}
