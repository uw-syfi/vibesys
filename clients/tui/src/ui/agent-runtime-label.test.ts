import {describe, expect, it} from 'bun:test';
import {agentRuntimeLabel} from '../agent-runtime-label.js';

describe('agentRuntimeLabel', () => {
  it('formats a known provider with its prettified model', () => {
    expect(agentRuntimeLabel('codex', 'gpt-5.1-codex-max')).toBe('Codex (GPT 5.1 Codex Max)');
  });

  it('maps each known provider to its display name', () => {
    expect(agentRuntimeLabel('claude', 'claude-sonnet-4-6')).toBe(
      'Claude Code (Claude Sonnet 4 6)',
    );
    expect(agentRuntimeLabel('gemini', null)).toBe('Gemini');
    expect(agentRuntimeLabel('opencode', null)).toBe('Opencode');
  });

  it('title-cases an unrecognized provider', () => {
    expect(agentRuntimeLabel('mystery_cli', 'v1')).toBe('Mystery Cli (V1)');
  });

  it('shows only the model when the provider is missing', () => {
    expect(agentRuntimeLabel(null, 'gpt-5.1-codex-max')).toBe('GPT 5.1 Codex Max');
    expect(agentRuntimeLabel(undefined, 'gpt-5.1-codex-max')).toBe('GPT 5.1 Codex Max');
  });

  it('shows only the provider when the model is missing', () => {
    expect(agentRuntimeLabel('codex', null)).toBe('Codex');
  });

  it('returns null when both are missing', () => {
    expect(agentRuntimeLabel(null, null)).toBeNull();
    expect(agentRuntimeLabel(undefined, undefined)).toBeNull();
    expect(agentRuntimeLabel('', '')).toBeNull();
  });
});
