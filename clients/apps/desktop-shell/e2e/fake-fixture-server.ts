/**
 * A fake run server that replays a run fixture (`@vibesys/run-fixtures`), behind the Fake bridge:
 * each `connectRun` hands the page a port speaking the main process's relay protocol; `subscribe`
 * answers the events the fixture's first batch holds, or, past `after_sequence`, those the run has
 * sent since (so a redial after `fakeHost.drop()` gets what it missed); `fakeHost.emit(events)`
 * sends more as a live batch; `readEvents` serves a finished run's history page by page.
 * `installFakeFixtureServer` runs in the page after `installFakeBridge`, so it is self-contained.
 */

export interface FixtureServerSeed {
  readonly runId: string;
  /** The events the first batch holds. */
  readonly first: readonly {readonly sequence?: number}[];
  /** A finished run's recorded events, which `readEvents` serves; absent: no history. */
  readonly history?: readonly {readonly sequence?: number}[];
  /** The snapshot's overview: the run's objective. */
  readonly overview: unknown;
}

export function installFakeFixtureServer(seed: FixtureServerSeed): void {
  type Event = {readonly sequence?: number};
  const encoder = new TextEncoder();
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const sent: Event[] = [...seed.first];
  const last = () => sent.at(-1)?.sequence ?? 0;
  let stream: ((message: unknown) => void) | null = null;
  let hangUp: (() => void) | null = null;
  const batch = (events: readonly Event[]) => ({
    type: 'event_batch',
    events,
    through_sequence: events.at(-1)?.sequence ?? 0,
    store_id: 'store-1',
    history_after_sequence: 0,
  });
  const answer = (request: {type: string; request_id?: string; after_sequence?: number}) => {
    const id = request.request_id ?? 'request';
    const envelope = {protocol_version: 1, request_id: id, timestamp: '2026-10-15T16:30:51Z'};
    if (request.type === 'subscribe') {
      const missed = sent.filter(event => (event.sequence ?? 0) > (request.after_sequence ?? 0));
      const head = {
        type: 'subscribed',
        request_id: id,
        run_id: seed.runId,
        latest_sequence: last(),
      };
      return missed.length === 0 ? [head] : [head, batch(missed)];
    }
    if (request.type === 'query.snapshot') {
      const snapshot = {
        run_id: seed.runId,
        sequence: 0,
        status: 'running',
        overview: seed.overview,
      };
      return [{...envelope, ok: true, snapshot}];
    }
    return [{...envelope, ok: false, error: `the fixture run does not answer ${request.type}`}];
  };
  const serve = (port: MessagePort): void => {
    const post = (message: unknown): void => {
      const data = JSON.stringify(message);
      port.postMessage({type: 'message', data, bytes: encoder.encode(data).length + 1});
    };
    port.onmessage = event => {
      const message = event.data as {type: string; data?: string};
      if (message.type === 'close') port.close();
      if (message.type !== 'send' || message.data === undefined) return;
      const request = JSON.parse(message.data) as {type: string};
      if (request.type === 'subscribe') {
        stream = post;
        hangUp = () => {
          port.postMessage({type: 'close'});
          port.close();
        };
      }
      for (const reply of answer(request)) post(reply);
    };
    port.postMessage({type: 'open', creditBytes: 16 * 1024 * 1024});
  };
  const history = (from: number, max: number) => {
    if (seed.history === undefined) return ok({kind: 'unsupported'});
    const page = seed.history.filter(event => (event.sequence ?? 0) >= from).slice(0, max);
    const next = (page.at(-1)?.sequence ?? from - 1) + 1;
    const complete = next > (seed.history.at(-1)?.sequence ?? 0);
    return ok({kind: 'page', events: page, next, complete});
  };
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(target.vibesysWelcome, {
    attach: () => ok(null),
    releaseRun: () => ok(null),
    connectRun: (id: string) => {
      const channel = new MessageChannel();
      serve(channel.port2);
      window.postMessage({type: 'vibesys:run-port', id}, '*', [channel.port1]);
    },
    onWake: () => undefined,
    readEvents: (_host: string, _project: string, _run: string, from: number, max: number) =>
      history(from, max),
  });
  Object.assign(target.fakeHost, {
    /** Streams more of the run as one live batch. */
    emit: (events: readonly Event[]) => {
      if (events.length === 0) return;
      sent.push(...events);
      stream?.(batch(events));
    },
    /** Drops the subscribed stream: the page sees its connection end and redials. */
    drop: () => hangUp?.(),
  });
}
