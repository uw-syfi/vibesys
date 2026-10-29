/** Match an OpenSSH Host pattern list: only `*`, `?`, and leading negation are special. */
export function sshHostPatternsMatch(patterns: readonly string[], host: string): boolean {
  let positive = false;
  for (const entry of patterns) {
    const negative = entry.startsWith('!');
    const pattern = negative ? entry.slice(1) : entry;
    if (!wildcardMatches(pattern.toLowerCase(), host.toLowerCase(), false)) continue;
    if (negative) return false;
    positive = true;
  }
  return positive;
}

/** Match one filename component using glob(3)'s portable subset, never recursive globstar. */
export function sshGlobSegmentMatches(pattern: string, value: string): boolean {
  if (value.includes('/') || value.includes('\\')) return false;
  if (value.startsWith('.') && !pattern.startsWith('.')) return false;
  return wildcardMatches(pattern, value, true);
}

function wildcardMatches(pattern: string, value: string, bracketExpressions: boolean): boolean {
  const tokens = wildcardTokens(Array.from(pattern), bracketExpressions);
  const characters = Array.from(value);
  let tokenIndex = 0;
  let valueIndex = 0;
  let starIndex = -1;
  let starValueIndex = -1;

  while (valueIndex < characters.length) {
    const token = tokens[tokenIndex];
    if (token?.kind === 'star') {
      starIndex = tokenIndex;
      starValueIndex = valueIndex;
      tokenIndex += 1;
    } else if (token !== undefined && tokenMatches(token, characters[valueIndex] ?? '')) {
      tokenIndex += 1;
      valueIndex += 1;
    } else if (starIndex >= 0) {
      starValueIndex += 1;
      valueIndex = starValueIndex;
      tokenIndex = starIndex + 1;
    } else {
      return false;
    }
  }

  while (tokens[tokenIndex]?.kind === 'star') tokenIndex += 1;
  return tokenIndex === tokens.length;
}

type WildcardToken =
  | {readonly kind: 'any'}
  | {readonly kind: 'bracket'; readonly expression: BracketExpression}
  | {readonly kind: 'literal'; readonly value: string}
  | {readonly kind: 'star'};

function wildcardTokens(
  pattern: readonly string[],
  bracketExpressions: boolean,
): readonly WildcardToken[] {
  const tokens: WildcardToken[] = [];
  for (let index = 0; index < pattern.length; index += 1) {
    const parsed = wildcardTokenAt(pattern, index, bracketExpressions);
    if (parsed.token.kind !== 'star' || tokens.at(-1)?.kind !== 'star') tokens.push(parsed.token);
    index = parsed.next - 1;
  }
  return tokens;
}

function wildcardTokenAt(
  pattern: readonly string[],
  index: number,
  bracketExpressions: boolean,
): {readonly next: number; readonly token: WildcardToken} {
  const character = pattern[index] ?? '';
  if (character === '*') return {next: index + 1, token: {kind: 'star'}};
  if (character === '?') return {next: index + 1, token: {kind: 'any'}};
  if (character === '[' && bracketExpressions) {
    const expression = bracketExpression(pattern, index);
    if (expression !== null) return {next: expression.next, token: {kind: 'bracket', expression}};
  }
  return {next: index + 1, token: {kind: 'literal', value: character}};
}

function tokenMatches(token: WildcardToken, character: string): boolean {
  switch (token.kind) {
    case 'any':
      return true;
    case 'bracket':
      return bracketIncludes(token.expression, character);
    case 'literal':
      return token.value === character;
    case 'star':
      return false;
  }
}

interface BracketExpression {
  readonly body: readonly string[];
  readonly negative: boolean;
  readonly next: number;
}

function bracketExpression(pattern: readonly string[], start: number): BracketExpression | null {
  let contentStart = start + 1;
  if (pattern[contentStart] === '!') contentStart += 1;
  if (pattern[contentStart] === ']') contentStart += 1;
  const end = pattern.indexOf(']', contentStart);
  if (end < 0 || end === start + 1) return null;
  const content = pattern.slice(start + 1, end);
  const negative = content[0] === '!';
  return {body: negative ? content.slice(1) : content, negative, next: end + 1};
}

function bracketIncludes(expression: BracketExpression, character: string): boolean {
  let included = false;
  for (let index = 0; index < expression.body.length; index += 1) {
    const first = expression.body[index] ?? '';
    const last = expression.body[index + 2];
    if (expression.body[index + 1] === '-' && last !== undefined) {
      if (first <= character && character <= last) included = true;
      index += 2;
    } else if (first === character) included = true;
  }
  return expression.negative ? !included : included;
}
