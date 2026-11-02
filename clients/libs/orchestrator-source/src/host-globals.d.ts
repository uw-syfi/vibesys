/**
 * Globals every JavaScript host has (the browser, Node, Bun) that the ES library does not
 * declare. Only what this package uses is declared, so a neutral entry stays free of the DOM and
 * Node libraries.
 */
declare class TextEncoder {
  encode(input: string): Uint8Array;
}

declare class TextDecoder {
  constructor(label?: string, options?: {fatal?: boolean});
  decode(input: Uint8Array): string;
}
