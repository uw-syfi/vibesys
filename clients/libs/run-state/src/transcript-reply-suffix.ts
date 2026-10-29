/** Find a structured reply at the end of streamed prose, independent of JSON formatting. */
function equalJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (typeof left !== 'object' || left === null || typeof right !== 'object' || right === null)
    return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = left as Record<string, unknown>;
  const b = right as Record<string, unknown>;
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length &&
    keys.every(key => Object.hasOwn(b, key) && equalJson(a[key], b[key]))
  );
}

function escaped(text: string, at: number): boolean {
  let slashes = 0;
  while (at > 0 && text[--at] === '\\') slashes += 1;
  return slashes % 2 === 1;
}

function bracketDepth(char: string | undefined): number {
  if (char === '}' || char === ']') return 1;
  return char === '{' || char === '[' ? -1 : 0;
}

function suffixStart(text: string): number | null {
  if (!text.endsWith('}') && !text.endsWith(']')) return null;
  let depth = 0;
  let quoted = false;
  for (let at = text.length - 1; at >= 0; at -= 1) {
    const char = text[at];
    if (char === '"' && !escaped(text, at)) quoted = !quoted;
    if (quoted) continue;
    depth += bracketDepth(char);
    if (depth === 0) return at;
  }
  return null;
}

/** Return the prose before an equivalent direct value or reply envelope; preserve unrelated text. */
export function beforeStructuredReply(text: string, value: unknown): string | undefined {
  const start = suffixStart(text);
  if (start === null) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.slice(start));
  } catch {
    return undefined;
  }
  const wrapped =
    typeof parsed === 'object' &&
    parsed !== null &&
    Object.keys(parsed).length === 1 &&
    Object.hasOwn(parsed, 'reply')
      ? (parsed as Record<string, unknown>)['reply']
      : undefined;
  return equalJson(parsed, value) || equalJson(wrapped, value)
    ? text.slice(0, start).trimEnd()
    : undefined;
}
