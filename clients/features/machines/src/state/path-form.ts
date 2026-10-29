/**
 * A checkout path field with suggestions: as the user types, the directory the path names is listed
 * on the machine (`paths.complete`, through `@vibesys/hosts`' completion reducer), and every listed
 * entry that is a VibeSys checkout is inspected once (`checkout.inspect`) so its suggestion carries
 * its version. Tokens come from the screen store's counter, so a reply to a field that was closed
 * and opened again can never be taken for the new one's.
 */
import {
  type CompletionResult,
  type CompletionState,
  completionStep,
  type HostKey,
  suggestions,
} from '@vibesys/hosts';
import type {CheckoutInspection} from '@vibesys/ipc';

/** What one checkout suggestion's inspection established, or the token it awaits. */
type Inspected =
  | {readonly kind: 'awaiting'; readonly token: number}
  | {readonly kind: 'inspected'; readonly inspection: CheckoutInspection}
  | {readonly kind: 'failed'};

export interface PathForm {
  readonly checkout: string;
  /** The host the field completes on, or null while none is named. */
  readonly host: HostKey | null;
  readonly completion: CompletionState;
  /** Each listed checkout's inspection, by its path. */
  readonly inspected: Readonly<Record<string, Inspected>>;
}

/** What a field asks of the machine: a directory listing or a checkout's inspection. */
export type PathFormRequest =
  | {
      readonly kind: 'complete';
      readonly token: number;
      readonly host: HostKey;
      readonly directory: string;
    }
  | {
      readonly kind: 'inspect';
      readonly token: number;
      readonly host: HostKey;
      readonly path: string;
    };

/** A field's next state, the requests it makes, and the last token it drew. */
export interface PathFormStep {
  readonly form: PathForm;
  readonly requests: readonly PathFormRequest[];
  readonly lastToken: number;
}

const FIELD = 'checkout';

/** An empty field on `host` whose tokens follow `lastToken`. */
export function pathForm(host: HostKey | null, checkout: string, lastToken: number): PathFormStep {
  const empty: PathForm = {
    checkout: '',
    host,
    completion: {next: lastToken + 1, fields: {}},
    inspected: {},
  };
  return typed(empty, checkout, lastToken);
}

/** The field after the user typed `checkout` (or named another `host`). */
export function typed(form: PathForm, checkout: string, lastToken: number): PathFormStep {
  if (form.host === null) return {form: {...form, checkout}, requests: [], lastToken};
  const step = completionStep(
    {...form.completion, next: Math.max(form.completion.next, lastToken + 1)},
    {type: 'input', field: FIELD, host: form.host, value: checkout},
  );
  const requests = step.requests.map(request => ({
    kind: 'complete' as const,
    token: request.token,
    host: request.host,
    directory: request.directory,
  }));
  return {
    form: {...form, checkout, completion: step.state},
    requests,
    lastToken: Math.max(lastToken, step.state.next - 1),
  };
}

/** The token the field's listing awaits, or null. */
function awaitedListing(form: PathForm): number | null {
  return form.completion.fields[FIELD]?.awaiting ?? null;
}

/** The field with its listing's reply, asking to inspect each checkout listed; null when stale. */
export function listed(
  form: PathForm,
  token: number,
  result: CompletionResult,
  lastToken: number,
): PathFormStep | null {
  if (form.host === null || awaitedListing(form) !== token) return null;
  const completion = completionStep(form.completion, {
    type: 'reply',
    field: FIELD,
    token,
    result,
  }).state;
  const entries = result.ok && result.value.kind === 'listed' ? result.value.entries : [];
  const inspected: Record<string, Inspected> = {...form.inspected};
  const requests: PathFormRequest[] = [];
  let last = lastToken;
  for (const entry of entries) {
    if (!entry.checkout || inspected[entry.path] !== undefined) continue;
    last += 1;
    inspected[entry.path] = {kind: 'awaiting', token: last};
    requests.push({kind: 'inspect', token: last, host: form.host, path: entry.path});
  }
  return {form: {...form, completion, inspected}, requests, lastToken: last};
}

/** The field with one inspection's reply; null when no suggestion awaits it. */
export function inspectedReply(
  form: PathForm,
  path: string,
  token: number,
  inspection: CheckoutInspection | null,
): PathForm | null {
  const known = form.inspected[path];
  if (known?.kind !== 'awaiting' || known.token !== token) return null;
  const next: Inspected = inspection === null ? {kind: 'failed'} : {kind: 'inspected', inspection};
  return {...form, inspected: {...form.inspected, [path]: next}};
}

/** Every token the field awaits, by slot. */
export function formAwaited(form: PathForm, prefix: string): Record<string, number | null> {
  const awaited: Record<string, number | null> = {[`${prefix}:listing`]: awaitedListing(form)};
  for (const [path, inspected] of Object.entries(form.inspected)) {
    awaited[`${prefix}:inspect:${path}`] = inspected.kind === 'awaiting' ? inspected.token : null;
  }
  return awaited;
}

/** How a checkout suggestion reads beside its path: its version, or why it cannot be used. */
function versionLabel(inspected: Inspected | undefined): string | undefined {
  if (inspected?.kind !== 'inspected') return undefined;
  const {inspection} = inspected;
  switch (inspection.state) {
    case 'found': {
      const commit = inspection.commit === null ? [] : [inspection.commit.slice(0, 7)];
      const fit = inspection.compatible ? [] : ['not usable by this app'];
      return [inspection.version, ...commit, ...fit].join(' · ');
    }
    case 'too_old':
      return `${inspection.version || 'unversioned'} · too old for this app`;
    case 'not_found':
    case 'unsupported':
      return undefined;
  }
}

/** The suggestions the field offers now, each with its version when it is a checkout. */
export function formSuggestions(
  form: PathForm,
): readonly {readonly value: string; readonly detail?: string}[] {
  return suggestions(form.completion, FIELD).map(value => {
    const detail = versionLabel(form.inspected[value.replace(/\/$/, '')]);
    return detail === undefined ? {value} : {value, detail};
  });
}
