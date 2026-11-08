package core

import "time"

// This file holds the accounting of setup apart from the run. Run.Usage stays the total of every turn
// the run has had; Run.SetupUsage is the part of it that belongs to setup, so what the run's limits
// count is the difference. A turn belongs to the phase it started in: the setup lead's turn that is in
// flight when setup completes is setup spend when it ends, and no turn that started in setup is ever
// optimize spend. SetupUsage is nil until the run leaves setup, and for a run that never had one.

// startedInSetup says a turn that started at the given time began before the run left setup.
func (r Run) startedInSetup(at time.Time) bool {
	return !r.OptimizeStartedAt.IsZero() && at.Before(r.OptimizeStartedAt)
}

// optimizeUsage is what the run's limits count: its usage less its setup's, field by field.
func (r Run) optimizeUsage() Usage {
	if r.SetupUsage == nil {
		return r.Usage
	}
	return r.Usage.Minus(*r.SetupUsage)
}

// Minus returns u less o, field by field and never below zero. A field u does not report stays
// unreported, and one o does not report leaves u's as it is.
func (u Usage) Minus(o Usage) Usage {
	sub := func(a, b *uint64) *uint64 {
		if a == nil || b == nil {
			return a
		}
		d := uint64(0)
		if *a > *b {
			d = *a - *b
		}
		return &d
	}
	out := Usage{
		InputTokens:         sub(u.InputTokens, o.InputTokens),
		CacheReadTokens:     sub(u.CacheReadTokens, o.CacheReadTokens),
		CacheCreationTokens: sub(u.CacheCreationTokens, o.CacheCreationTokens),
		OutputTokens:        sub(u.OutputTokens, o.OutputTokens),
		ReasoningTokens:     sub(u.ReasoningTokens, o.ReasoningTokens),
		CostUSD:             u.CostUSD,
	}
	if u.CostUSD != nil && o.CostUSD != nil {
		d := max(0, *u.CostUSD-*o.CostUSD)
		out.CostUSD = &d
	}
	return out
}

// within says every field u reports is at most o's, and o reports it too: u is a part of o.
func (u Usage) within(o Usage) bool {
	le := func(a, b *uint64) bool { return a == nil || b != nil && *a <= *b }
	return le(u.InputTokens, o.InputTokens) && le(u.CacheReadTokens, o.CacheReadTokens) &&
		le(u.CacheCreationTokens, o.CacheCreationTokens) && le(u.OutputTokens, o.OutputTokens) &&
		le(u.ReasoningTokens, o.ReasoningTokens) &&
		(u.CostUSD == nil || o.CostUSD != nil && *u.CostUSD <= *o.CostUSD)
}
