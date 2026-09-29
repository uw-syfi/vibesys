/**
 * Run notes: private text per run, kept by the home server in the TUI's file
 * (`GET/PUT /api/notes/{run}`, last write wins). A note reaches an agent only when the user puts it
 * in a composer and sends it.
 */
import {
  type Dispatch,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
} from 'react';

export interface NoteRecord {
  runId: string;
  text: string;
  createdAt: string;
  updatedAt: string;
}

export interface NotesApi {
  get(runId: string): Promise<NoteRecord | null>;
  /** `keepalive` lets the request outlive the page (the `pagehide` save). */
  put(runId: string, text: string, keepalive?: boolean): Promise<NoteRecord>;
}

const errorText = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

export function httpNotesApi(token: string, fetcher: typeof fetch = fetch): NotesApi {
  const call = async (runId: string, init: RequestInit = {}): Promise<NoteRecord | null> => {
    const writes = init.body !== undefined;
    const response = await fetcher(`/api/notes/${encodeURIComponent(runId)}`, {
      ...init,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(writes ? {'Content-Type': 'application/json'} : {}),
      },
    });
    const body = (await response.json().catch(() => ({}))) as {
      note?: NoteRecord | null;
      error?: {message?: string};
    };
    if (!response.ok) {
      throw new Error(body.error?.message ?? `Notes are unavailable (HTTP ${response.status})`);
    }
    return body.note ?? null;
  };
  return {
    get: runId => call(runId),
    put: async (runId, text, keepalive = false) => {
      const note = await call(runId, {method: 'PUT', body: JSON.stringify({text}), keepalive});
      if (note === null) throw new Error('The home server returned no note.');
      return note;
    },
  };
}

export type NoteState =
  | {phase: 'unavailable'}
  | {phase: 'loading'; runId: string}
  | {phase: 'failed'; runId: string; message: string}
  /** `saved` is the text the server last confirmed; the note is unsaved while `text` differs. */
  | {phase: 'ready'; runId: string; text: string; saved: string; error: string | null};

export type NoteAction =
  | {type: 'load'; runId: string}
  | {type: 'loaded'; runId: string; text: string}
  | {type: 'loadFailed'; runId: string; message: string}
  | {type: 'edit'; text: string}
  | {type: 'saved'; runId: string; text: string}
  | {type: 'saveFailed'; runId: string; message: string};

export const INITIAL_NOTE: NoteState = {phase: 'loading', runId: ''};

/** Results for another run than the one shown are dropped; nothing edits a note that is not loaded. */
export function noteReducer(state: NoteState, action: NoteAction): NoteState {
  if (action.type === 'load') return {phase: 'loading', runId: action.runId};
  if (state.phase === 'unavailable' || state.phase === 'failed') return state;
  if (action.type === 'edit')
    return state.phase === 'ready' ? {...state, text: action.text} : state;
  if (action.runId !== state.runId) return state;
  switch (action.type) {
    case 'loaded':
      return {
        phase: 'ready',
        runId: action.runId,
        text: action.text,
        saved: action.text,
        error: null,
      };
    case 'loadFailed':
      return {phase: 'failed', runId: action.runId, message: action.message};
    case 'saved':
      return state.phase === 'ready' ? {...state, saved: action.text, error: null} : state;
    case 'saveFailed':
      return state.phase === 'ready' ? {...state, error: action.message} : state;
  }
}

/** Saves one at a time, in call order, so a slow earlier save never lands after a later one. */
export function serialSaver(api: NotesApi): (runId: string, text: string) => Promise<NoteRecord> {
  let tail: Promise<unknown> = Promise.resolve();
  return (runId, text) => {
    const next = tail.then(() => api.put(runId, text));
    tail = next.catch(() => undefined);
    return next;
  };
}

/** Both composers are single-line inputs, which drop line breaks: fold them into spaces. */
export function asDraft(text: string): string {
  return text.trim().replace(/\s*\n\s*/g, ' ');
}

export interface NoteController {
  state: NoteState;
  edit: (text: string) => void;
  /** Saves unsaved text now (the editor lost focus). */
  flush: () => void;
  retry: () => void;
}

const SAVE_DELAY_MS = 500;
const UNAVAILABLE: NoteState = {phase: 'unavailable'};

function useFlush(
  api: NotesApi | null,
  latest: RefObject<NoteState>,
  dispatch: Dispatch<NoteAction>,
): (keepalive?: boolean) => void {
  const save = useMemo(() => (api === null ? null : serialSaver(api)), [api]);
  return useCallback(
    (keepalive = false) => {
      const note = latest.current;
      if (api === null || save === null || note.phase !== 'ready' || note.text === note.saved)
        return;
      const {runId, text} = note;
      // ponytail: the pagehide save skips the queue (the page is going away), so a slower queued save
      // can still land after it; a note over fetch's 64 KiB keepalive limit fails there.
      const put = keepalive ? api.put(runId, text, true) : save(runId, text);
      put.then(
        () => dispatch({type: 'saved', runId, text}),
        (error: unknown) => dispatch({type: 'saveFailed', runId, message: errorText(error)}),
      );
    },
    [api, save, latest, dispatch],
  );
}

/**
 * The note of the run on screen: loaded on its first view (`wanted`), saved 500 ms after typing
 * stops, on blur, before another run's note loads, and on `pagehide`.
 */
export function useNote(
  api: NotesApi | null,
  runId: string | null,
  wanted: boolean,
): NoteController {
  const [state, dispatch] = useReducer(noteReducer, INITIAL_NOTE);
  const latest = useRef(state);
  latest.current = state;
  const flush = useFlush(api, latest, dispatch);
  const load = useCallback(
    (run: string) => {
      if (api === null) return;
      dispatch({type: 'load', runId: run});
      api.get(run).then(
        note => dispatch({type: 'loaded', runId: run, text: note?.text ?? ''}),
        (error: unknown) => dispatch({type: 'loadFailed', runId: run, message: errorText(error)}),
      );
    },
    [api],
  );
  const target = api !== null && wanted && runId !== null ? runId : null;
  useEffect(() => {
    const note = latest.current;
    if (
      target === null ||
      (note.phase !== 'unavailable' && note.phase !== 'failed' && note.runId === target)
    )
      return;
    flush();
    load(target);
  }, [target, flush, load]);
  const text = state.phase === 'ready' ? state.text : null;
  useEffect(() => {
    if (text === null) return;
    const timer = setTimeout(() => flush(), SAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [text, flush]);
  useEffect(() => {
    const onHide = () => flush(true);
    addEventListener('pagehide', onHide);
    return () => removeEventListener('pagehide', onHide);
  }, [flush]);
  return {
    state: api === null ? UNAVAILABLE : state,
    edit: next => dispatch({type: 'edit', text: next}),
    flush: () => flush(),
    retry: () => {
      if (target !== null) load(target);
    },
  };
}
