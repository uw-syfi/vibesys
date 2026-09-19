/**
 * Lowercase member name of a generated numeric enum value, for labels
 * (`RoundReviewVerdict.PASS` is `pass`). Null for an absent or unspecified value.
 */
export function enumWord(names: Record<number, string>, value: number | undefined): string | null {
  if (value === undefined || value === 0) return null;
  return names[value]?.toLowerCase() ?? null;
}
