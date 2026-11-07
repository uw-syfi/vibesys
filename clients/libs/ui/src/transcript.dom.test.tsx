/**
 * A `Transcript`: its rows read in order with the thinking and tool calls folded, opening one tells
 * the caller (so a body read on demand can be asked for), the rows before the first are asked for by
 * a button and by a scroll near the top, only rows that arrive at the end draw in, and what is long
 * stays inside its row.
 */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {Transcript, type TranscriptEarlier, type TranscriptItem} from './index.js';

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

const said = (id: string, text = id): TranscriptItem => ({kind: 'said', id, text});
const thought = (id: string): TranscriptItem => ({
  kind: 'thought',
  id,
  label: 'Thought for 14s',
  text: 'Because the gate is memory bound.',
});
const tool = (
  id: string,
  extra: Partial<Extract<TranscriptItem, {kind: 'tool'}>> = {},
): TranscriptItem => ({
  kind: 'tool',
  id,
  name: 'Command',
  state: 'ok',
  summary: 'ls',
  input: null,
  output: 'a\nb',
  ...extra,
});

interface Props {
  readonly items: readonly TranscriptItem[];
  readonly earlier?: TranscriptEarlier | null;
  readonly opened?: (id: string, open: boolean) => void;
}

const view = (props: Props) => (
  <Transcript
    label="A conversation"
    items={props.items}
    {...(props.earlier === undefined ? {} : {earlier: props.earlier})}
    {...(props.opened === undefined ? {} : {opened: props.opened})}
  />
);

async function mounted(props: Props) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(view(props)));
  return {container, root, show: (next: Props) => act(async () => root.render(view(next)))};
}

const rows = (container: HTMLElement) => [...container.querySelectorAll('.vs-transcript-row')];

describe('a transcript', () => {
  test('keeps its rows in order, thinking and a call folded to one line', async () => {
    const {container} = await mounted({items: [said('a'), thought('b'), tool('c'), said('d')]});
    expect(rows(container).map(row => row.getAttribute('data-kind'))).toEqual([
      'said',
      'thought',
      'tool',
      'said',
    ]);
    expect(container.textContent).toContain('Thought for 14s');
    expect(container.querySelectorAll('.vs-collapsible-body')).toHaveLength(0);
    expect(container.querySelectorAll('[aria-expanded="true"]')).toHaveLength(0);
  });

  test('opening a thought or a call opens its body and says so, and closing says that too', async () => {
    const log: string[] = [];
    const {container} = await mounted({
      items: [thought('b'), tool('c')],
      opened: (id, open) => log.push(`${id}:${open}`),
    });
    const heads = [...container.querySelectorAll('button[aria-expanded]')] as HTMLElement[];
    await act(async () => heads[0]?.click());
    await act(async () => heads[1]?.click());
    expect(container.textContent).toContain('Because the gate is memory bound.');
    await act(async () => heads[1]?.click());
    expect(log).toEqual(['b:true', 'c:true', 'c:false']);
  });

  test('says what stands in for a result that has not been read, when the call is opened', async () => {
    const {container} = await mounted({
      items: [tool('c', {output: null, notice: 'Loading 12 KB'})],
    });
    await act(async () =>
      (container.querySelector('button[aria-expanded]') as HTMLElement).click(),
    );
    expect(container.textContent).toContain('Loading 12 KB');
  });

  test('asks for earlier rows by its button, unless they are being read', async () => {
    const loads: string[] = [];
    const earlier = (loading: boolean): TranscriptEarlier => ({
      text: 'Earlier turns',
      loading,
      load: () => loads.push('load'),
    });
    const idle = await mounted({items: [said('a')], earlier: earlier(false)});
    const button = [...idle.container.querySelectorAll('button')].find(
      one => one.textContent === 'Earlier turns',
    ) as HTMLElement;
    await act(async () => button.click());
    expect(loads).toEqual(['load']);
    const busy = await mounted({items: [said('a')], earlier: earlier(true)});
    const waiting = [...busy.container.querySelectorAll('button')].find(
      one => one.textContent === 'Earlier turns',
    );
    expect(waiting?.getAttribute('aria-disabled')).toBe('true');
    const none = await mounted({items: [said('a')], earlier: null});
    expect(none.container.textContent).not.toContain('Earlier turns');
  });

  test('asks for earlier rows when scrolled near the top, and not when scrolled away from it', async () => {
    const loads: string[] = [];
    const earlier: TranscriptEarlier = {
      text: 'Earlier',
      loading: false,
      load: () => loads.push('x'),
    };
    const {container} = await mounted({items: [said('a'), said('b')], earlier});
    const pane = container.querySelector('.vs-transcript-pane') as HTMLElement;
    pane.scrollTop = 2_000;
    await act(async () => pane.dispatchEvent(new UIEvent('scroll', {bubbles: true})));
    expect(loads).toEqual([]);
    pane.scrollTop = 0;
    await act(async () => pane.dispatchEvent(new UIEvent('scroll', {bubbles: true})));
    expect(loads).toEqual(['x']);
  });

  test('draws in only a row that arrives at the end, not the first rows and not older ones', async () => {
    const first = [said('b'), said('c')];
    const {container, show} = await mounted({items: first});
    expect(container.querySelectorAll('[data-entering]')).toHaveLength(0);
    await show({items: [said('a'), ...first]});
    expect(container.querySelectorAll('[data-entering]')).toHaveLength(0);
    await show({items: [said('a'), ...first, said('d')]});
    expect(rows(container).map(row => row.hasAttribute('data-entering'))).toEqual([
      false,
      false,
      false,
      true,
    ]);
  });

  test('keeps the rows a reader is looking at where they were when older ones arrive', async () => {
    const first = [said('b'), said('c')];
    const {container, show} = await mounted({items: first});
    const pane = container.querySelector('.vs-transcript-pane') as HTMLElement;
    pane.scrollTop = 40;
    await act(async () => pane.dispatchEvent(new UIEvent('scroll', {bubbles: true})));
    const before = pane.scrollTop;
    await show({items: [said('a'), ...first]});
    expect(pane.scrollTop).toBeGreaterThanOrEqual(before);
  });

  test('draws a link row with its title, chip and trail, and pressing the title leads on', async () => {
    const log: string[] = [];
    const {container} = await mounted({
      items: [
        {
          kind: 'link',
          id: 'l',
          icon: 'bot',
          title: 'Spawned a helper',
          chip: {label: 'Done', tone: 'ok'},
          trail: '2:10 AM',
          press: () => log.push('go'),
        },
      ],
    });
    expect(container.textContent).toContain('Done');
    expect(container.textContent).toContain('2:10 AM');
    const title = [...container.querySelectorAll('button')].find(
      one => one.textContent === 'Spawned a helper',
    );
    await act(async () => (title as HTMLElement).click());
    expect(log).toEqual(['go']);
  });
});
