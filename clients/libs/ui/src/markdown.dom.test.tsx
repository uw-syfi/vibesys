/** `Markdown` and `markdownToPlain` through `.` only: what renders, what never does, where links go. */

import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {Window} from 'happy-dom';
import {act, type ReactNode} from 'react';
import {createRoot} from 'react-dom/client';
import {
  LinkOpener,
  Markdown,
  Message,
  markdownToPlain,
  Said,
  StructuredReply,
  Thought,
} from './index.js';

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

async function mount(node: ReactNode): Promise<HTMLElement> {
  const container = document.createElement('div');
  document.body.append(container);
  await act(async () => createRoot(container).render(node));
  return container;
}

function one(container: HTMLElement, selector: string): HTMLElement {
  const element = container.querySelector<HTMLElement>(selector);
  if (element === null) throw new Error(`no ${selector}`);
  return element;
}

const noop = (): void => undefined;

describe('Markdown', () => {
  test('draws lists, code, emphasis, strikethrough and tables', async () => {
    const container = await mount(
      <Markdown
        text={[
          'Plan:',
          '',
          '- one',
          '- two with `code`',
          '',
          '1. first',
          '2. second',
          '',
          '~~gone~~ and **bold** and *soft*',
          '',
          '```ts',
          'const x = 1;',
          '```',
          '',
          '| a | b |',
          '| - | - |',
          '| 1 | 2 |',
        ].join('\n')}
      />,
    );
    expect(container.querySelectorAll('ul > li')).toHaveLength(2);
    expect(container.querySelectorAll('ol > li')).toHaveLength(2);
    expect(one(container, 'li code').textContent).toBe('code');
    expect(one(container, 'del').textContent).toBe('gone');
    expect(one(container, 'strong').textContent).toBe('bold');
    expect(one(container, 'em').textContent).toBe('soft');
    expect(one(container, 'pre.vs-markdown-pre code').textContent).toContain('const x = 1;');
    expect(container.querySelectorAll('.vs-markdown-scroll table th')).toHaveLength(2);
    expect(container.textContent).not.toContain('**');
  });

  test('headings are bold lines at the size of their level, never heading elements', async () => {
    const container = await mount(<Markdown text={'# a\n\n## b\n\n### c\n\n###### d'} />);
    const levels = [...container.querySelectorAll('.vs-markdown-heading')].map(heading =>
      heading.getAttribute('data-level'),
    );
    expect(levels).toEqual(['1', '2', '3', '6']);
    expect(container.querySelector('h1, h2, h3, h4, h5, h6')).toBeNull();
    expect(container.textContent?.replace(/\s/g, '')).toBe('abcd');
  });

  test('raw HTML is text: no element is injected, whatever the markup', async () => {
    const hostile = [
      '<img src="x" onerror="alert(1)">',
      '<script>alert(1)</script>',
      '<b>counters</b>',
      '<a href="javascript:alert(1)" onclick="x()">go</a>',
      '<iframe src="https://example.com"></iframe>',
    ];
    for (const html of hostile) {
      const container = await mount(<Markdown text={`before ${html} after`} />);
      expect(container.querySelector('img, script, b, iframe, [onerror], [onclick]')).toBeNull();
      expect(container.querySelector('a')).toBeNull();
      expect(container.textContent).toContain(html.slice(0, 5));
    }
    const block = await mount(<Markdown text={'<div onclick="x()">block</div>\n\ntext'} />);
    expect(block.querySelector('[onclick]')).toBeNull();
    expect(block.textContent).toContain('<div onclick="x()">');
  });

  test('a web or mail link opens through the opener and never navigates', async () => {
    const opened: string[] = [];
    const container = await mount(
      <LinkOpener open={url => opened.push(url)}>
        <Markdown text="[docs](https://example.com/a?b=1) and https://example.org and [mail](mailto:a@b.co)" />
      </LinkOpener>,
    );
    const anchors = [...container.querySelectorAll('a')];
    expect(anchors.map(a => a.getAttribute('href'))).toEqual([
      'https://example.com/a?b=1',
      'https://example.org',
      'mailto:a@b.co',
    ]);
    for (const anchor of anchors) {
      expect(anchor.getAttribute('rel')).toContain('noreferrer');
      const event = new MouseEvent('click', {bubbles: true, cancelable: true});
      await act(async () => {
        anchor.dispatchEvent(event);
      });
      expect(event.defaultPrevented).toBe(true);
    }
    expect(opened).toEqual(['https://example.com/a?b=1', 'https://example.org', 'mailto:a@b.co']);
  });

  test('without an opener a link still carries no referrer and opens in a new context', async () => {
    const container = await mount(<Markdown text="[docs](https://example.com)" />);
    const anchor = one(container, 'a');
    expect(anchor.getAttribute('target')).toBe('_blank');
    expect(anchor.getAttribute('rel')).toBe('noreferrer noopener');
  });

  test('only web and mail addresses are links: scripts and relative paths are their text', async () => {
    const container = await mount(
      <Markdown text="[a](javascript:alert(1)) [b](./local.md) [c](#frag) [d](file:///etc/passwd) [e](data:text/html,x)" />,
    );
    expect(container.querySelector('a')).toBeNull();
    expect(container.textContent).toBe('a b c d e');
  });

  test('an image is not loaded: its description shows', async () => {
    const container = await mount(
      <Markdown text="![a chart](https://tracker.example/p.png) and ![](https://x.example/q.png)" />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.textContent).toBe('a chart and image');
  });

  test('Thought, Said, replies and reasoning read as Markdown; prompts stay plain', async () => {
    const container = await mount(
      <>
        <Thought text="**title**" open toggle={noop} />
        <Said text="- item" />
        <Message author="a" variant="reply" text="- m1" open toggle={noop} />
        <Message author="a" variant="prompt" text="- raw **p**" open toggle={noop} />
        <StructuredReply
          author="a"
          title="Reply"
          fields={[
            {kind: 'prose', key: 'p', label: 'P', text: '**why**'},
            {kind: 'list', key: 'l', label: 'L', items: ['`one`']},
          ]}
          raw="{}"
          open
          toggle={noop}
        />
      </>,
    );
    expect(one(container, '.vs-thought strong').textContent).toBe('title');
    expect(one(container, '.vs-said li').textContent).toBe('item');
    expect(one(container, '.vs-message[data-variant="reply"] li').textContent).toBe('m1');
    expect(one(container, '.vs-message[data-variant="prompt"]').textContent).toContain(
      '- raw **p**',
    );
    expect(one(container, '.vs-reply-field strong').textContent).toBe('why');
    expect(one(container, '.vs-prose-list code').textContent).toBe('one');
  });

  test('a closed message previews its first line as plain words', async () => {
    const container = await mount(
      <Message
        author="a"
        variant="reply"
        text={'## **Plan** for `x`\n\nlater'}
        open={false}
        toggle={noop}
      />,
    );
    expect(container.textContent).toContain('Plan for x');
    expect(container.textContent).not.toMatch(/[*`#]/);
  });
});

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const PIECES = [
  '**',
  '*',
  '`',
  '```',
  '~~',
  '__',
  '_',
  '# ',
  '## ',
  '> ',
  '- ',
  '* ',
  '1. ',
  '| ',
  ' | ',
  '---',
  '[',
  ']',
  '(',
  ')',
  '](',
  '![',
  '<',
  '>',
  '\\',
  '\n',
  '\n\n',
  ' ',
  'word',
  'snake_case',
  'a',
  'https://x.io',
];

function arbitrary(random: () => number): string {
  const length = Math.floor(random() * 14);
  let text = '';
  for (let piece = 0; piece < length; piece += 1) {
    text += PIECES[Math.floor(random() * PIECES.length)];
  }
  return text;
}

describe('markdownToPlain', () => {
  test('property: the output is one line with no markdown control sequence, and is a fixed point', () => {
    const random = generator(20_261_017);
    for (let run = 0; run < 3000; run += 1) {
      const input = arbitrary(random);
      const plain = markdownToPlain(input);
      const context = JSON.stringify(input);
      expect(plain, context).not.toMatch(/\n/);
      expect(plain, context).not.toMatch(/\*|`|~~|!?\[[^\]\n]*\]\([^)\n]*\)/);
      expect(plain, context).not.toMatch(/^(?:#{1,6}(?:\s|$)|>|[-+*](?:\s|$)|\d+[.)](?:\s|$))/);
      expect(plain, context).toBe(plain.trim());
      expect(markdownToPlain(plain), context).toBe(plain);
    }
  });

  test('property: text with no markup keeps its words, only the spacing between them collapses', () => {
    const random = generator(7);
    const words = ['alpha', 'beta', 'snake_case', 'x.y', 'a-b', '3', 'ok?'];
    for (let run = 0; run < 500; run += 1) {
      const picked = Array.from(
        {length: 1 + Math.floor(random() * 6)},
        () => words[Math.floor(random() * words.length)] as string,
      );
      expect(markdownToPlain(picked.join(random() < 0.5 ? ' ' : '\n  '))).toBe(picked.join(' '));
    }
  });

  test('reads the constructs an agent writes as the words they hold', () => {
    expect(markdownToPlain('**Plan**: pad the `counters` ([why](https://x.io))')).toBe(
      'Plan: pad the counters (why)',
    );
    expect(markdownToPlain('## Heading\n- [x] done\n> quoted\n1. one')).toBe(
      'Heading done quoted one',
    );
    expect(markdownToPlain('| a | b |\n| - | - |\n| 1 | 2 |')).toBe('a b 1 2');
    expect(markdownToPlain('cut mid **emphasi')).toBe('cut mid emphasi');
  });
});
