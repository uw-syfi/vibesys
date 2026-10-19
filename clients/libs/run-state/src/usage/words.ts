/** How a count of tokens is worded: the one place a count is shortened. */

const SCALES = [
  {unit: 'k', size: 1_000},
  {unit: 'M', size: 1_000_000},
  {unit: 'B', size: 1_000_000_000},
] as const;

/** One decimal, with a whole number written without its `.0`. */
function oneDecimal(value: number): string {
  const text = value.toFixed(1);
  return text.endsWith('.0') ? text.slice(0, -2) : text;
}

/** A count in a line: whole below a thousand, else thousands, millions or billions, one decimal. */
export function compactTokens(count: number): string {
  if (count < SCALES[0].size) return String(count);
  // The smallest scale whose rounded value stays under 1000, so 999 949 is `999.9k` and 999 950 `1M`.
  for (const scale of SCALES) {
    const rounded = Math.round((count / scale.size) * 10) / 10;
    if (rounded < 1000 || scale === SCALES[SCALES.length - 1]) {
      return `${oneDecimal(rounded)}${scale.unit}`;
    }
  }
  return String(count);
}
