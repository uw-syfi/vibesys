/**
 * Globals every JavaScript host has (the browser, Node, Bun) that the ES library does not
 * declare. Only what this package uses is declared, so a neutral entry stays free of the DOM and
 * Node libraries.
 */
interface AbortSignal {
  readonly aborted: boolean;
  addEventListener(type: 'abort', listener: () => void, options?: {once?: boolean}): void;
}

declare class AbortController {
  readonly signal: AbortSignal;
  abort(): void;
}
