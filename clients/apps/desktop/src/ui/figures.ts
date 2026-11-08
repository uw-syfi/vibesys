/**
 * The one way the app writes a measured value as a figure, so a number reads the same on the Runs
 * list, the report's tiles and chart, and Catch me up: four significant digits (`17.01`, `118.4`,
 * `0.5123`) and a suffix for thousands (`11.08k`). The whole value is the text on hover.
 */

/** A number as a figure: four significant digits and a suffix for thousands. */
export function formatFigure(value: number): string {
  // Rounded to its four figures first, so a value that rounds up into the next unit (999,999 is
  // 1.000M, not 1000k) picks that unit's suffix rather than the one its unrounded size suggests.
  const rounded = Number(value.toPrecision(4));
  const size = Math.abs(rounded);
  const [divisor, suffix] =
    size >= 1e9 ? [1e9, 'G'] : size >= 1e6 ? [1e6, 'M'] : size >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = Number((rounded / divisor).toPrecision(4));
  return `${scaled === 0 ? 0 : scaled}${suffix}`;
}
