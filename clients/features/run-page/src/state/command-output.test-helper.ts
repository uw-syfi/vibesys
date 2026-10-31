/**
 * Text as a command prints it, for property tests: any characters (combining marks, emoji, CJK),
 * lines joined by LF or CRLF, a trailing newline, only newlines, the empty text, and text at and
 * far over the 64 KiB a record holds inline. `fc.string()` alone never leaves printable ASCII or
 * passes ten characters, so a property over it cannot reach the cases that break line handling.
 */
import * as fc from 'fast-check';

const lines = fc.array(fc.string({unit: 'grapheme', maxLength: 12}), {maxLength: 6});

export const commandOutput: fc.Arbitrary<string> = fc.oneof(
  fc.string({unit: 'grapheme', maxLength: 40}),
  lines.map(each => each.join('\n')),
  lines.map(each => each.join('\r\n')),
  lines.map(each => `${each.join('\n')}\n`),
  fc.nat(5).map(count => '\n'.repeat(count)),
  fc.constant(''),
  fc.constantFrom('x'.repeat(65_536), 'é'.repeat(40_000), 'line\n'.repeat(30_000)),
);
