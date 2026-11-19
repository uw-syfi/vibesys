import {describe, expect, it} from '@effect/vitest';
import type {TradeoffReport} from '../../../state/index.js';
import {installDom, mount} from '../../../testing/dom/harness.js';
import {TradeOff} from './trade-off.js';

installDom();

function tradeoff(names: readonly string[]): TradeoffReport {
  return {
    objectives: names.map(name => ({name, better: 'higher'})),
    points: [],
    frontierCount: 0,
  };
}

describe('the trade-off chart axes', () => {
  it("reset to the new report's own objectives instead of aliasing both axes to the same one", () => {
    const screen = mount(
      <TradeOff tradeoff={tradeoff(['Alpha', 'Beta', 'Gamma'])} objectives={[]} />,
    );
    const axisValues = (): string[] =>
      [...screen.container.querySelectorAll('select')].map(select => select.value);
    expect(axisValues()).toEqual(['Alpha', 'Beta']);

    // The same instance (no remount) now shows a different run's report, with an unrelated
    // objective set: neither chosen name is in it any more.
    screen.render(<TradeOff tradeoff={tradeoff(['Delta', 'Epsilon', 'Zeta'])} objectives={[]} />);

    // Falls back to this report's own first two objectives, one axis each; it must not collapse
    // both axes onto the same objective.
    expect(axisValues()).toEqual(['Delta', 'Epsilon']);
    screen.unmount();
  });
});
