import {
  Card,
  ChatMessage,
  Collapsible,
  Message,
  type MessageVariant,
  type ReplyField,
  Stack,
  StructuredReply,
  Text,
  Thread,
  type ThreadItem,
  ToolCall,
  TurnResult,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ComponentName, GalleryEntry} from './gallery-entry.js';

const noop = (): void => undefined;

function CollapsibleDemo(): JSX.Element {
  const [first, setFirst] = useState(true);
  const [second, setSecond] = useState(false);
  return (
    <Stack>
      <Collapsible label="Open block" open={first} toggle={setFirst}>
        <Text>What the block holds, shown while it is open.</Text>
      </Collapsible>
      <Collapsible
        label="Closed block"
        summary="A one-line preview of what is inside"
        open={second}
        toggle={setSecond}
      >
        <Text>Hidden until it is opened.</Text>
      </Collapsible>
    </Stack>
  );
}

const REPLY =
  'The head and tail counters share a cache line, so every enqueue invalidates the dequeuer’s copy.\nI will pad each counter to its own line and re-measure.';

function MessageDemo(): JSX.Element {
  const [shown, setShown] = useState<ReadonlySet<MessageVariant>>(new Set(['reply', 'note']));
  const entry = (variant: MessageVariant, author: string, text: string, tone?: 'ok' | 'err') => (
    <Message
      key={variant + author}
      author={author}
      variant={variant}
      {...(tone === undefined ? {} : {tone})}
      text={text}
      open={shown.has(variant)}
      toggle={open => {
        const next = new Set(shown);
        if (open) next.add(variant);
        else next.delete(variant);
        setShown(next);
      }}
    />
  );
  return (
    <Stack>
      {entry('reply', 'Worker', REPLY)}
      {entry('reasoning', 'Worker', 'Padding costs memory but removes the false sharing.')}
      {entry('prompt', 'Worker', 'Make the queue faster without changing its public API.')}
      {entry('note', 'Gate', 'accuracy check passed in 41s', 'ok')}
    </Stack>
  );
}

function ToolCallDemo(): JSX.Element {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set(['ok', 'failed']));
  const call = (id: string, props: Parameters<typeof ToolCall>[0]) => (
    <ToolCall
      key={id}
      {...props}
      open={open.has(id)}
      toggle={want => {
        const next = new Set(open);
        if (want) next.add(id);
        else next.delete(id);
        setOpen(next);
      }}
    />
  );
  const base = {open: false, toggle: noop};
  return (
    <Stack>
      {call('pending', {
        ...base,
        name: 'Bash',
        state: 'pending',
        summary: 'cargo bench --bench queue',
        input: '{\n  "command": "cargo bench --bench queue"\n}',
        output: null,
      })}
      {call('ok', {
        ...base,
        name: 'Read',
        state: 'ok',
        summary: 'src/queue.rs',
        meta: '4 lines',
        input: '{\n  "file_path": "src/queue.rs"\n}',
        output: 'pub struct Queue {\n    head: AtomicUsize,\n    tail: AtomicUsize,\n}',
      })}
      {call('failed', {
        ...base,
        name: 'Edit',
        state: 'failed',
        summary: 'src/queue.rs',
        meta: 'failed · 38 chars',
        input: '{\n  "file_path": "src/queue.rs",\n  "old_string": "head"\n}',
        output: 'The string to replace was not found.',
      })}
    </Stack>
  );
}

const REPLY_FIELDS: readonly ReplyField[] = [
  {kind: 'badge', key: 'verdict', label: 'Verdict', value: 'pass', tone: 'ok'},
  {
    kind: 'prose',
    key: 'analysis',
    label: 'Analysis',
    text: 'The current src/lib.rs matches the declared outcome.\nThe benchmark gate passed and no check regressed.',
  },
  {kind: 'list', key: 'checks', label: 'Checks', items: ['build', 'accuracy', 'benchmark']},
  {
    kind: 'group',
    key: 'skills_used',
    label: 'Skills used',
    fields: [{kind: 'fact', key: 'skill', label: 'Skill', value: 'rocprof'}],
  },
];

const THREAD: readonly ThreadItem[] = [
  {kind: 'heading', id: 'h1', title: 'Pad the head and tail counters', detail: 'Worker'},
  {
    kind: 'message',
    id: 'm0',
    author: 'Worker',
    variant: 'prompt',
    text: 'Make the queue faster without changing its public API.',
  },
  {
    kind: 'message',
    id: 'm1',
    author: 'Worker',
    variant: 'reasoning',
    text: 'Padding costs memory but removes the false sharing.',
  },
  {kind: 'message', id: 'm2', author: 'Worker', variant: 'reply', text: REPLY},
  {
    kind: 'tool',
    id: 't1',
    name: 'Read',
    state: 'ok',
    summary: 'src/queue.rs',
    meta: '4 lines',
    input: '{\n  "file_path": "src/queue.rs"\n}',
    output: 'pub struct Queue {\n    head: AtomicUsize,\n    tail: AtomicUsize,\n}',
  },
  {
    kind: 'tool',
    id: 't2',
    name: 'Edit',
    state: 'failed',
    summary: 'src/queue.rs',
    meta: 'failed · 38 chars',
    input: '{\n  "file_path": "src/queue.rs"\n}',
    output: 'The string to replace was not found.',
  },
  {
    kind: 'message',
    id: 'm3',
    author: 'Worker',
    variant: 'reply',
    text: 'The edit missed; I will read the file again before changing it.',
  },
  {
    kind: 'tool',
    id: 't3',
    name: 'Bash',
    state: 'pending',
    summary: 'cargo bench --bench queue',
    input: '{\n  "command": "cargo bench --bench queue"\n}',
    output: null,
  },
  {
    kind: 'reply',
    id: 'r1',
    author: 'Judge',
    title: 'Judge response',
    fields: REPLY_FIELDS,
    raw: '{}',
  },
  {kind: 'heading', id: 'h2', title: 'Batch the producer handoff', detail: 'Worker'},
  {
    kind: 'message',
    id: 'm4',
    author: 'Gate',
    variant: 'note',
    tone: 'ok',
    text: 'accuracy check passed in 41s',
  },
  {
    kind: 'node',
    id: 'n1',
    node: (
      <Card>
        <Text>A block of the caller’s own, with its own controls.</Text>
      </Card>
    ),
  },
  {
    kind: 'message',
    id: 'm5',
    author: 'Worker',
    variant: 'reply',
    text: 'The batch handoff is slower under contention, so I am dropping it.',
    notice: 'The event was cut at the server’s size bound.',
  },
];

function StructuredReplyDemo(): JSX.Element {
  const [open, setOpen] = useState(true);
  return (
    <StructuredReply
      author="Judge"
      title="Judge response"
      fields={REPLY_FIELDS}
      raw={'{\n  "verdict": "pass"\n}'}
      open={open}
      toggle={setOpen}
    />
  );
}

function TurnResultDemo(): JSX.Element {
  return (
    <Stack>
      <TurnResult
        label="Plan"
        author="Orchestrator"
        headline="Preallocate fixed-size queue slots"
        context={{prefix: 'builds on', label: 'Batch the producer handoff', press: noop}}
        sections={[
          {
            kind: 'prose',
            key: 'hypothesis',
            label: 'Hypothesis',
            text: 'Allocating a node per push puts the allocator on the hot path; a fixed ring of preallocated slots removes it and keeps producers on separate cache lines.',
          },
          {
            kind: 'prose',
            key: 'expected_effect',
            label: 'Expected effect',
            text: '1.3x to 1.6x more pushes per second at eight producers.',
          },
          {
            kind: 'prose',
            key: 'pass_criteria',
            label: 'Pass criteria',
            text: 'All queue tests pass and the benchmark reports a gain over its base.',
          },
          {
            kind: 'list',
            key: 'hypothesis_updates',
            label: 'Updates to other hypotheses',
            items: ['pad-counters: parked. The gain was inside the noise.'],
          },
        ]}
        more={[
          {kind: 'fact', key: 'hypothesis_id', label: 'Hypothesis id', value: 'fixed-slot-ring'},
        ]}
        raw={'{\n  "title": "Preallocate fixed-size queue slots"\n}'}
      />
      <TurnResult
        label="Judge response"
        author="Judge"
        chips={[{label: 'pass', tone: 'ok'}]}
        context={{prefix: 'builds on', label: 'input'}}
        sections={[
          {kind: 'prose', key: 'analysis', label: 'Analysis', text: 'The change holds under test.'},
        ]}
        more={[]}
        raw="{}"
      />
    </Stack>
  );
}

export const THREAD_ENTRIES: Readonly<
  Record<
    Extract<
      ComponentName,
      | 'ChatMessage'
      | 'Collapsible'
      | 'Message'
      | 'ToolCall'
      | 'StructuredReply'
      | 'Thread'
      | 'TurnResult'
    >,
    GalleryEntry
  >
> = {
  ChatMessage: {
    render: () => (
      <Stack>
        <ChatMessage side="end" text="What is the best idea so far?" />
        <ChatMessage side="start" text={REPLY} notice="Writing" />
        <ChatMessage
          side="start"
          tone="err"
          text="The provider refused the request: quota exhausted."
          notice="Failed"
        />
      </Stack>
    ),
  },
  Collapsible: {render: () => <CollapsibleDemo />},
  Message: {render: () => <MessageDemo />},
  ToolCall: {render: () => <ToolCallDemo />},
  StructuredReply: {render: () => <StructuredReplyDemo />},
  TurnResult: {render: () => <TurnResultDemo />},
  Thread: {
    render: () => (
      <Card>
        <Thread label="Turn by turn" items={THREAD} />
      </Card>
    ),
  },
};
