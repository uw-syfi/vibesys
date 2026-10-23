/** The MessagePort subset used by gateway IPC, with FIFO scheduling and close-as-truncation. */
export class FakeGatewayPort {
  #peer: FakeGatewayPort | undefined;
  #closed = false;
  #started = false;
  readonly #buffer: unknown[] = [];
  readonly #listeners = new Map<string, Set<(...args: unknown[]) => void>>();
  #pageListener: ((data: unknown) => void) | undefined;

  constructor(
    private readonly post: (lane: object, run: () => void) => void,
    private readonly clone: (value: unknown) => unknown,
  ) {}

  static pair(post: (lane: object, run: () => void) => void, clone: (value: unknown) => unknown) {
    const page = new FakeGatewayPort(post, clone);
    const main = new FakeGatewayPort(post, clone);
    page.#peer = main;
    main.#peer = page;
    return {page, main};
  }

  on(event: string, listener: (...args: unknown[]) => void): this {
    const listeners = this.#listeners.get(event) ?? new Set();
    listeners.add(listener);
    this.#listeners.set(event, listeners);
    return this;
  }

  /** Like a web `MessagePort`: assigning a handler starts the port. */
  set onmessage(handler: ((event: {data: unknown}) => void) | null) {
    this.#pageListener = handler === null ? undefined : data => handler({data});
    if (handler !== null) this.start();
  }

  start(): void {
    this.#started = true;
    for (const value of this.#buffer.splice(0)) this.#deliver(value);
  }

  postMessage(value: unknown): void {
    if (this.#closed) return;
    const peer = this.#peer;
    const copy = this.clone(value);
    if (peer)
      this.post(peer, () => {
        if (peer.#closed) return;
        if (!peer.#started) peer.#buffer.push(copy);
        else peer.#deliver(copy);
      });
  }

  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    const peer = this.#peer;
    if (peer)
      this.post(peer, () => {
        if (!peer.#closed) peer.#emit('close');
      });
  }

  #deliver(data: unknown): void {
    this.#pageListener?.(data);
    this.#emit('message', {data});
  }

  #emit(event: string, ...args: unknown[]): void {
    for (const listener of this.#listeners.get(event) ?? []) listener(...args);
  }
}
