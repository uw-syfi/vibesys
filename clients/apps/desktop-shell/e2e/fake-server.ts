/**
 * A fake run server behind the Fake bridge, for the Run screen's live journey: `attach` binds
 * nothing but is recorded, and each `connectRun` hands the page a port (as the preload does, in a
 * window message) whose other end speaks the main process's relay protocol (credit, then one
 * message per frame) and, inside it, enough of the run protocol for a live session: `subscribe`
 * answers `subscribed` and one batch, `query.snapshot` the seeded snapshot, and Pause and Resume an
 * acknowledgement. What the page asked is recorded in order on `window.fakeHost.commands`.
 * `installFakeServer` runs in the page after `installFakeBridge`, so it is self-contained.
 */

export interface FakeServerSeed {
  readonly runId: string;
  /** The stream's one batch (an `event_batch` server message). */
  readonly batch: unknown;
  /** The snapshot `query.snapshot` answers. */
  readonly snapshot: unknown;
}

export function installFakeServer(seed: FakeServerSeed): void {
  const commands: string[] = [];
  const encoder = new TextEncoder();
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const answer = (request: {type: string; request_id?: string}): unknown[] => {
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
      case 'command.resume':
        commands.push(request.type);
        return [{...envelope, ok: true, ack: {action: request.type.slice(8), status: 'pending'}}];
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
      for (const reply of answer(JSON.parse(message.data))) post(reply);
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
    // This fake records no history: an ended run reads that its host does not serve one.
    readEvents: () => ok({kind: 'unsupported'}),
  });
  Object.assign(target.fakeHost, {commands});
}
