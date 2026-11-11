import type {Fault} from './fault.js';

/** What the host refused a latest-value topic for (`TopicFailureCode`). */
export type TopicRefusal = 'unsupported' | 'over-limit' | 'source-failed' | 'internal';

/** How one stream of a latest-value topic ended. */
export type TopicEnd =
  | {readonly _tag: 'Refused'; readonly refusal: TopicRefusal}
  | {readonly _tag: 'Silent'}
  | {readonly _tag: 'Closed'}
  | {readonly _tag: 'Invalid'; readonly detail: string}
  | {readonly _tag: 'Failed'; readonly fault: Fault};

export type TopicRetry = 'backoff' | 'lost' | 'next-link';

const linkDown = (fault: Fault): boolean =>
  fault.kind !== 'FellBehind' && (fault.retryable || fault.kind === 'NoBridge');

/** Whether to retry on this link, redial it, or wait for a later link. */
export function topicRetry(end: TopicEnd): TopicRetry {
  switch (end._tag) {
    case 'Refused':
      return end.refusal === 'unsupported' ? 'next-link' : 'backoff';
    case 'Silent':
    case 'Closed':
      return 'backoff';
    case 'Invalid':
      return 'next-link';
    case 'Failed':
      if (end.fault.kind === 'FellBehind') return 'backoff';
      return linkDown(end.fault) ? 'lost' : 'next-link';
  }
}
