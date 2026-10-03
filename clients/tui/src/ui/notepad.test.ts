import {describe, expect, it} from 'bun:test';
import {formatRunLabel} from './notepad.js';

describe('formatRunLabel', () => {
  it('replaces the generated id shape with the slug and a plain UTC timestamp', () => {
    expect(formatRunLabel('20260901-160434-429a8605-bad-cpp')).toBe(
      'bad-cpp · 2026-09-01 16:04 UTC',
    );
  });

  it('keeps every dash-separated part of a multi-word slug', () => {
    expect(formatRunLabel('20260101-000000-00000000-multi-word-slug')).toBe(
      'multi-word-slug · 2026-01-01 00:00 UTC',
    );
  });

  it('does not mistake a slug that itself looks like a timestamp for a parse failure', () => {
    expect(formatRunLabel('20260901-160434-429a8605-bad-cpp-20260901-160433')).toBe(
      'bad-cpp-20260901-160433 · 2026-09-01 16:04 UTC',
    );
  });

  it('returns an id that does not match the generator shape unchanged', () => {
    expect(formatRunLabel('run-1')).toBe('run-1');
    expect(formatRunLabel('run')).toBe('run');
  });
});
