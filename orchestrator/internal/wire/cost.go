package wire

import (
	"unicode/utf8"

	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
)

// The limits of what the cost message carries of the models, which are the limits the contract
// states for it.
const (
	MaxCostModels     = 64
	MaxModelNameBytes = 256
	// MaxRunNameBytes is the limit on the name of a run in the host's list of runs by cost.
	MaxRunNameBytes = 256
)

// CostMessage returns what a run has cost as the contract states it, current to the given log
// sequence. Every list keeps the order the view has: the contract promises it, so a client shows the
// view in the order it is given.
//
// The models are the one list a run can make longer than the contract allows, and a name can be
// longer than it allows too. Models past MaxCostModels (the least costly, since the view is in cost
// order) are left out of the models and of every bucket's by_model, and a name longer than
// MaxModelNameBytes is cut at a character boundary; either sets models_truncated. Nothing that is not
// a list of models changes: the total and each bucket's cost still count every turn.
func CostMessage(v cost.View, sequence uint64) *pb.RunCost {
	out := &pb.RunCost{
		RunId: string(v.Run), Sequence: sequence, AsOf: when(v.AsOf), Total: costUsageOut(v.Total),
		Turns: v.Turns, UnpricedTurns: v.Unpriced, LastHourUsd: v.LastHour,
	}
	if v.MaxCostUSD > 0 {
		out.MaxCostUsd = &v.MaxCostUSD
	}
	out.Roles, out.Models, out.Series, out.ModelsTruncated = breakdownOut(v.Breakdown)
	return out
}

// HostCostMessage returns what the runs of a host have cost as the contract states it, current to the
// given log sequence. Its splits and series are stated as a run's are, by the same code, so the
// limits on models and their names hold here too and set models_truncated the same way. The runs are
// in the order the view has them, at most cost.MaxHostRuns of them, and the rest are in other_runs.
func HostCostMessage(v cost.HostView, sequence uint64) *pb.HostCost {
	out := &pb.HostCost{
		Sequence: sequence, AsOf: when(v.AsOf), Total: costUsageOut(v.Total), RunCount: v.RunCount,
		Turns: v.Turns, UnpricedTurns: v.Unpriced, LastHourUsd: v.LastHour,
	}
	for _, r := range v.Runs {
		line := &pb.HostRunCost{
			RunId: string(r.Run), Name: cut(r.Name, MaxRunNameBytes), Status: runStatuses.out(r.Status),
			Turns: r.Turns, UnpricedTurns: r.Unpriced, Usage: costUsageOut(r.Usage), TotalTokens: r.Tokens,
		}
		if r.MaxCostUSD > 0 {
			line.MaxCostUsd = &r.MaxCostUSD
		}
		out.Runs = append(out.Runs, line)
	}
	if o := v.Others; o.Runs > 0 {
		out.OtherRuns = &pb.OtherRunsCost{
			Runs: o.Runs, Turns: o.Turns, UnpricedTurns: o.Unpriced, Usage: costUsageOut(o.Usage), TotalTokens: o.Tokens,
		}
	}
	out.Roles, out.Models, out.Series, out.ModelsTruncated = breakdownOut(v.Breakdown)
	return out
}

// breakdownOut states the lists of a Breakdown as the contract does: the types, the models and the
// series, with the models past the limit left out of the models and of every bucket's by_model, and
// whether it left a model out or cut a name.
func breakdownOut(b cost.Breakdown) (roleList []*pb.RoleCost, modelList []*pb.ModelCost, series *pb.SpendSeries, truncated bool) {
	for _, r := range b.Roles {
		roleList = append(roleList, &pb.RoleCost{
			Role: roles.out(r.Role), Agents: r.Agents, Turns: r.Turns, UnpricedTurns: r.Unpriced, Usage: costUsageOut(r.Usage),
			TotalTokens: r.Tokens,
		})
	}
	kept := map[cost.ModelKey]bool{}
	for _, m := range b.Models {
		if len(kept) == MaxCostModels {
			truncated = true
			break
		}
		kept[m.Model] = true
		modelList = append(modelList, &pb.ModelCost{
			Model: modelRefOut(m.Model, &truncated), Turns: m.Turns, UnpricedTurns: m.Unpriced,
			Usage: costUsageOut(m.Usage), TotalTokens: m.Tokens,
		})
	}
	series = &pb.SpendSeries{Start: when(b.Series.Start), Width: durationpb.New(b.Series.Width)}
	for _, bucket := range b.Series.Buckets {
		out := &pb.SpendBucket{Start: when(bucket.Start), CostUsd: bucket.Cost}
		for _, m := range bucket.ByModel {
			if kept[m.Model] {
				out.ByModel = append(out.ByModel, &pb.ModelSpend{Model: modelRefOut(m.Model, &truncated), CostUsd: m.Cost})
			}
		}
		series.Buckets = append(series.Buckets, out)
	}
	return roleList, modelList, series, truncated
}

// cut is s cut to at most limit bytes at a character boundary.
func cut(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	n := limit
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}

// modelRefOut is the contract's message for a model. A name over the limit is cut at a character
// boundary, and truncated says so.
func modelRefOut(m cost.ModelKey, truncated *bool) *pb.ModelRef {
	name := cut(string(m.Model), MaxModelNameBytes)
	*truncated = *truncated || len(name) < len(m.Model)
	return &pb.ModelRef{Harness: harnesses.out(m.Harness), Model: name}
}

// costUsageOut is a usage that is always a message, so that "nothing reported" is an empty usage a
// client reads, never a missing one.
func costUsageOut(u core.Usage) *pb.Usage {
	return &pb.Usage{
		InputTokens: u.InputTokens, CacheReadTokens: u.CacheReadTokens, CacheCreationTokens: u.CacheCreationTokens,
		OutputTokens: u.OutputTokens, ReasoningTokens: u.ReasoningTokens, CostUsd: u.CostUSD,
	}
}
