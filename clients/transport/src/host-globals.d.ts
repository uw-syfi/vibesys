/**
 * The timer and UUID globals of every runtime this entry runs in (browsers, Node, Bun, Electron),
 * which the ES library does not declare; the DOM and Node libraries both do. Declaring the few
 * members the package calls keeps the entry runtime neutral rather than borrowing either library,
 * so a Node or DOM API used here is still a type error.
 */
interface Crypto {
  randomUUID(): string;
}

declare var crypto: Crypto;

declare function setTimeout(callback: () => void, delayMs: number): unknown;

declare function clearTimeout(handle: unknown): void;
