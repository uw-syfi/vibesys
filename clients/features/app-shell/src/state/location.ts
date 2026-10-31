/**
 * Where a window is: the one location, which the sidebar's store holds and the window's address
 * writes down, so an entry of the window's history restores all of it. What a screen was opened
 * for (New run's machine or run to repeat, Settings' section) is an instruction carried out on the
 * way there, not a place: the address leaves it out, so going Back does not carry it out again.
 */
import type {HostKey} from '@vibesys/hosts';
import {parseRunRef, type RunRef} from '@vibesys/runs';

/** The screen the main area shows. */
export type Screen =
  /**
   * New run; opened for a machine (Machines' "New run here"), it starts on that machine; opened
   * to run `again` a run, it starts from that run's place and options.
   */
  | {readonly kind: 'new-run'; readonly machine?: HostKey; readonly again?: RunRef}
  | {readonly kind: 'machines'}
  /** Settings; opened for a machine's `section` (New run's link to set a machine up), it lands there. */
  | {
      readonly kind: 'settings';
      readonly machine?: HostKey;
      readonly section?: 'clusters' | 'logins';
    }
  /** The Runs dashboard, the app's home. */
  | {readonly kind: 'runs'}
  | {readonly kind: 'usage'}
  /** A run; `zoom` is where its page is zoomed, which only the page reads. */
  | {readonly kind: 'run'; readonly run: RunRef; readonly zoom?: string};

const PAGES = ['new-run', 'machines', 'settings', 'usage'] as const;
const RUN_KEY = 'run=';

/** `screen` as the address's fragment; home is the bare address the app starts on. */
export function fragmentOf(screen: Screen): string {
  switch (screen.kind) {
    case 'runs':
      return '';
    case 'run': {
      const {host, runId} = screen.run;
      const named = `${RUN_KEY}${encodeURIComponent(host)}/${encodeURIComponent(runId)}`;
      return screen.zoom === undefined || screen.zoom === '' ? named : `${named}&${screen.zoom}`;
    }
    default:
      return screen.kind;
  }
}

/** The run a fragment's `host/run` names, or null when it names none. */
function namedRun(named: string): RunRef | null {
  const [host, runId, extra] = named.split('/');
  if (host === undefined || runId === undefined || extra !== undefined) return null;
  try {
    return parseRunRef(
      {host: decodeURIComponent(host), runId: decodeURIComponent(runId)},
      'address',
    );
  } catch {
    return null;
  }
}

/** The screen a fragment names. Only this app writes fragments, so any other is home. */
export function screenOf(fragment: string): Screen {
  const page = PAGES.find(each => each === fragment);
  if (page !== undefined) return {kind: page};
  if (!fragment.startsWith(RUN_KEY)) return {kind: 'runs'};
  const rest = fragment.slice(RUN_KEY.length);
  const at = rest.indexOf('&');
  const run = namedRun(at < 0 ? rest : rest.slice(0, at));
  if (run === null) return {kind: 'runs'};
  return at < 0 ? {kind: 'run', run} : {kind: 'run', run, zoom: rest.slice(at + 1)};
}
