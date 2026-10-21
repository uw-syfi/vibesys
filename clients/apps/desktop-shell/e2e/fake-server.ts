/**
 * A fake run server behind the Fake bridge, for the Run screen's live journey: `attach` binds
 * nothing but is recorded, and each `connectRun` hands the page a port (as the preload does, in a
 * window message) whose other end speaks the main process's relay protocol (credit, then one
 * message per frame) and, inside it, enough of the run protocol for a live session: `subscribe`
 * answers `subscribed` and one batch, `query.snapshot` the seeded snapshot, and Pause and Resume an
 * acknowledgement. With `quota`, the run is paused on a usage limit, as the run server does it: a
 * Resume when capacity returns is followed by `quota_waiting`, and a Pause or a Resume now while it
 * waits by `quota_wait_cancelled`. What the page asked is recorded in order on
 * `window.fakeHost.commands` (a Resume names a `when` other than `now`); `window.fakeHost.send`
 * posts a message to the subscribed stream (see `installFakeLive`). The fake records no history:
 * an ended run reads that its host serves none, and its helper offers nothing beyond the run's own
 * server (`helperFeatures`), so Chat is off once it ends.
 * `installFakeServer` runs in the page after `installFakeBridge`, so it is self-contained.
 */

export interface FakeServerSeed {
  readonly runId: string;
  /** The stream's one batch (an `event_batch` server message). */
  readonly batch: unknown;
  /** The snapshot `query.snapshot` answers. */
  readonly snapshot: unknown;
  /** The usage limit the run is paused on (its `quota_paused` is in `batch`). */
  readonly quota?: {readonly provider: string; readonly resumesAt: number};
}

export function installFakeServer(seed: FakeServerSeed): void {
  const commands: string[] = [];
  const encoder = new TextEncoder();
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  let [sequence, waiting] = [1, false];
  /** Sends to the page's subscribed stream, where the run's events go (not the command channel). */
  let stream: ((message: unknown) => void) | null = null;
  /** One event the run emits after the seeded batch, as its own batch. */
  const emitted = (data: {kind: string} & Record<string, unknown>): unknown => {
    sequence += 1;
    return {
      type: 'event_batch',
      events: [{timestamp: '2026-10-15T16:31:00Z', type: data.kind, sequence, data}],
      through_sequence: sequence,
      store_id: 'store-1',
      history_after_sequence: 0,
    };
  };
  /** What the run emits after acknowledging `type` (with `when`), while paused by `quota`. */
  const quotaEvents = (type: string, when: string | undefined): unknown[] => {
    const quota = seed.quota;
    if (quota === undefined) return [];
    if (type === 'command.resume' && when === 'capacity_returns') {
      waiting = true;
      return [
        emitted({kind: 'quota_waiting', provider: quota.provider, resumes_at: quota.resumesAt}),
      ];
    }
    if (!waiting) return [];
    waiting = false;
    const reason = type === 'command.pause' ? 'paused' : 'resumed';
    return [emitted({kind: 'quota_wait_cancelled', provider: quota.provider, reason})];
  };
  const answer = (request: {type: string; request_id?: string; when?: string}): unknown[] => {
    const id = request.request_id ?? 'request';
    const envelope = {protocol_version: 1, request_id: id, timestamp: '2026-10-15T16:30:51Z'};
    switch (request.type) {
      case 'subscribe':
        return [
          {type: 'subscribed', request_id: id, run_id: seed.runId, latest_sequence: 1},
          seed.batch,
        ];
      case 'query.snapshot':
        return [{...envelope, ok: true, snapshot: seed.snapshot}];
      case 'command.pause':
      case 'command.resume': {
        const {when} = request;
        commands.push(
          when === undefined || when === 'now' ? request.type : `${request.type}:${when}`,
        );
        const ack = {
          ...envelope,
          ok: true,
          ack: {action: request.type.slice(8), status: 'pending'},
        };
        for (const event of quotaEvents(request.type, when)) stream?.(event);
        return [ack];
      }
      default:
        return [{...envelope, ok: false, error: `the fake run does not answer ${request.type}`}];
    }
  };
  /** The main process's end of one relayed connection. */
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
      if (request.type === 'subscribe') stream = post;
      for (const reply of answer(request)) post(reply);
    };
    port.postMessage({type: 'open', creditBytes: 16 * 1024 * 1024});
  };
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(target.vibesysWelcome, {
    attach: (_host: string, instance: string) => {
      commands.push(`attach ${instance}`);
      return ok(null);
    },
    releaseRun: () => {
      commands.push('release');
      return ok(null);
    },
    connectRun: (id: string) => {
      const channel = new MessageChannel();
      serve(channel.port2);
      window.postMessage({type: 'vibesys:run-port', id}, '*', [channel.port1]);
    },
    onWake: () => undefined,
    helperFeatures: () => ok([]),
    readEvents: () => ok({kind: 'unsupported'}),
    readTrace: () => ok({kind: 'unsupported'}),
    readSummary: () => ok({kind: 'unsupported'}),
    readSpan: () => ok({kind: 'unsupported'}),
  });
  Object.assign(target.fakeHost, {commands, send: (message: unknown) => stream?.(message)});
}
