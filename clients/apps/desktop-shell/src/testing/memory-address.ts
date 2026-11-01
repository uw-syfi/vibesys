/** The run's location in memory: the zoom a navigation moved to, told to whoever follows it. */
export class MemoryAddress {
  #zoom = '';
  readonly #listeners = new Set<() => void>();
  readonly zoom = (): string => this.#zoom;
  readonly move = (zoom: string): void => {
    this.#zoom = zoom;
    for (const listener of this.#listeners) listener();
  };
  readonly subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };
  readonly onEscape = (): (() => void) => () => undefined;
}
