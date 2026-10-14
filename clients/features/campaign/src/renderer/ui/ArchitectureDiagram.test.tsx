import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {type ArchitectureGraphDocument, parseArchitectureGraph} from '@vibesys/contracts';
import {renderToStaticMarkup} from 'react-dom/server';
import {ArchitectureDiagram} from '../index.js';

const queue = fixture('architecture-queue.json');
const microservices = fixture('architecture-microservices.json');

test('renders cursor-aligned changes, recorded paths, and explicit missing attribution', () => {
  const html = render(queue, 'queue-ffi-bounds', 3);

  expect(html).toContain('data-component-id="queue.ffi" data-changed="true"');
  expect(html).toContain('data-component-id="queue.mutex" data-changed="false"');
  expect(html).toContain('C ABI, api, changed at selected measurement');
  expect(html).toContain('aria-haspopup="dialog"');
  expect(html).toMatch(/aria-controls="[^"]+-architecture-component-queue\.ffi"/);
  expect(html).toContain('src/ffi.rs');
  expect(html).toContain('<dt>Cost</dt><dd>Unavailable</dd>');
  expect(html).toContain('<dt>Latency</dt><dd>Unavailable</dd>');
});

test('component buttons expose instance-scoped accessible relationships', () => {
  const html = render(queue, 'queue-copy-elision', 2);
  const producerTrigger = html.match(
    /id="([^"]+-architecture-component-trigger-queue\.producer)"[^>]+aria-controls="([^"]+-architecture-component-queue\.producer)"/,
  );

  expect(producerTrigger).not.toBeNull();
  expect(producerTrigger?.[1]).not.toBe(producerTrigger?.[2]);
  expect(html).toContain('aria-expanded="false"');
});

test('the responsive graph is a named keyboard region with accessible relationships', () => {
  const html = render(microservices, 'hotel-pooled-client', 20);

  expect(html).toContain('<section class="architecture-scroll"');
  expect(html).toContain('aria-label="Scrollable system architecture graph"');
  expect(html).toContain('tabindex="0"');
  expect(html).toContain('aria-label="Architecture relationships"');
  expect(html).toContain('Frontend Search/Nearby Search');
  expect(html).toContain('0.004 USD/1k requests');
  expect(html).toContain('<dt>Cost</dt><dd>Unavailable</dd>');
});

test('a sparse cursor carries attribution but not stale change details forward', () => {
  const html = render(microservices, 'campaign-measurement-without-capture', 25, 'hotel.frontend');

  expect(html).toContain('data-component-id="hotel.frontend" data-changed="false"');
  expect(html).toContain('15.2 p95 ms');
  expect(html).not.toContain('services/frontend/client.go');
});

function render(
  graph: ArchitectureGraphDocument,
  cursorMeasurementId: string,
  cursorSequence: number,
  selectedComponentId: string | null = null,
): string {
  return renderToStaticMarkup(
    <ArchitectureDiagram
      graph={graph}
      cursorMeasurementId={cursorMeasurementId}
      cursorSequence={cursorSequence}
      selectedComponentId={selectedComponentId}
      onSelectedComponentId={() => undefined}
    />,
  );
}

function fixture(name: string): ArchitectureGraphDocument {
  const path = resolve(import.meta.dir, `../../fixtures/${name}`);
  return parseArchitectureGraph(JSON.parse(readFileSync(path, 'utf8')));
}
