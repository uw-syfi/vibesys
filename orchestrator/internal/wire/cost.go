package wire

import (
	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
)

// CostMessage returns what a run has cost as the contract states it, current to the given log
// sequence. Every list keeps the order the view has: the contract promises it, so a client shows the
// view in the order it is given.
func CostMessage(v cost.View, sequence uint64) *pb.RunCost {
	out := &pb.RunCost{
		RunId: string(v.Run), Sequence: sequence, AsOf: when(v.AsOf), Total: costUsageOut(v.Total),
		Turns: v.Turns, UnpricedTurns: v.Unpriced, LastHourUsd: v.LastHour,
		Series: &pb.SpendSeries{Start: when(v.Series.Start), Width: durationpb.New(v.Series.Width)},
	}
	if v.MaxCostUSD > 0 {
		out.MaxCostUsd = &v.MaxCostUSD
	}
	for _, r := range v.Roles {
		out.Roles = append(out.Roles, &pb.RoleCost{
			Role: roles.out(r.Role), Agents: r.Agents, Turns: r.Turns, UnpricedTurns: r.Unpriced, Usage: costUsageOut(r.Usage),
		})
	}
	for _, m := range v.Models {
		out.Models = append(out.Models, &pb.ModelCost{
			Model: modelRefOut(m.Model), Turns: m.Turns, UnpricedTurns: m.Unpriced, Usage: costUsageOut(m.Usage),
		})
	}
	for _, b := range v.Series.Buckets {
		bucket := &pb.SpendBucket{Start: when(b.Start), CostUsd: b.Cost}
		for _, m := range b.ByModel {
			bucket.ByModel = append(bucket.ByModel, &pb.ModelSpend{Model: modelRefOut(m.Model), CostUsd: m.Cost})
		}
		out.Series.Buckets = append(out.Series.Buckets, bucket)
	}
	return out
}

func modelRefOut(m cost.ModelKey) *pb.ModelRef {
	return &pb.ModelRef{Harness: harnesses.out(m.Harness), Model: string(m.Model)}
}

// costUsageOut is a usage that is always a message, so that "nothing reported" is an empty usage a
// client reads, never a missing one.
func costUsageOut(u core.Usage) *pb.Usage {
	return &pb.Usage{
		InputTokens: u.InputTokens, CacheReadTokens: u.CacheReadTokens, CacheCreationTokens: u.CacheCreationTokens,
		OutputTokens: u.OutputTokens, ReasoningTokens: u.ReasoningTokens, CostUsd: u.CostUSD,
	}
}
