/**
 * The one way the app writes a measured value as a figure, so a number reads the same on the Runs
 * list, the report's tiles and chart, and Catch me up: four significant digits (`17.01`, `118.4`,
 * `0.5123`) and a suffix for thousands (`11.08k`). The whole value is the text on hover.
 */

/** A number as a figure: four significant digits and a suffix for thousands. */
export function formatFigure(value: number): string {
  const size = Math.abs(value);
  const [divisor, suffix] =
    size >= 1e9 ? [1e9, 'G'] : size >= 1e6 ? [1e6, 'M'] : size >= 1e3 ? [1e3, 'k'] : [1, ''];
  const scaled = Number((value / divisor).toPrecision(4));
  return `${scaled === 0 ? 0 : scaled}${suffix}`;
}
