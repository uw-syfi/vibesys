/** A window's history in memory, for tests of the shell without a page. */
import type {WindowAddress} from '@vibesys/feature-app-shell/renderer';
import {fragmentOf, type Screen, screenOf} from '@vibesys/feature-app-shell/state';

export class FakeWindowAddress implements WindowAddress {
  readonly #listeners = new Set<(screen: Screen) => void>();
  entries: readonly string[] = [''];
  at = 0;

  readonly screen = (): Screen => screenOf(this.entries[this.at] ?? '');

  readonly push = (screen: Screen): void => {
    this.entries = [...this.entries.slice(0, this.at + 1), fragmentOf(screen)];
    this.at = this.entries.length - 1;
  };

  readonly back = (): void => {
    if (this.at === 0) return;
    this.at -= 1;
    for (const listener of this.#listeners) listener(this.screen());
  };

  readonly onRestore = (listener: (screen: Screen) => void): (() => void) => {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  };

  readonly onEscape = (): (() => void) => () => undefined;
}
