/**
 * The dense settings of the page parts: a card with less padding, and metric tiles packed on a
 * grid whose row count follows the width. Through the `.` entry only.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {Card, denseTilesPerRow, MetricTile, MetricTiles} from './index.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

describe('dense tiles', () => {
  test('two tiles share one row from the width of two, and every width in a window of 780 px and up', () => {
    expect(denseTilesPerRow(500, 2)).toBe(2);
    expect(denseTilesPerRow(660, 2)).toBe(2);
    expect(denseTilesPerRow(400, 2)).toBe(1);
    expect(denseTilesPerRow(660, 6)).toBe(3);
  });

  test('a row holds at least one and at most as many tiles as there are, and more width never fits fewer', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 0, max: 4000}),
        fc.integer({min: 0, max: 4000}),
        fc.integer({min: 1, max: 12}),
        (a, b, count) => {
          const [narrow, wide] = a < b ? [a, b] : [b, a];
          const few = denseTilesPerRow(narrow, count);
          const many = denseTilesPerRow(wide, count);
          expect(few).toBeGreaterThanOrEqual(1);
          expect(many).toBeLessThanOrEqual(count);
          expect(many).toBeGreaterThanOrEqual(few);
        },
      ),
    );
  });

  test('dense tiles and cards say so, and the default ones do not', async () => {
    const container = document.createElement('div');
    document.body.append(container);
    await act(async () =>
      createRoot(container).render(
        <>
          <MetricTiles label="Dense" count={1} dense>
            <MetricTile name="a" figure="1" />
          </MetricTiles>
          <MetricTiles label="Default" count={1}>
            <MetricTile name="b" figure="2" />
          </MetricTiles>
          <Card dense>Packed</Card>
          <Card>Roomy</Card>
        </>,
      ),
    );
    const dense = (selector: string): (boolean | undefined)[] =>
      [...container.querySelectorAll(selector)].map(each => each.hasAttribute('data-dense'));
    expect(dense('.vs-tiles')).toEqual([true, false]);
    expect(dense('.vs-card')).toEqual([true, false]);
  });
});
