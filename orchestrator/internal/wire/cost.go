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
		Series: &pb.SpendSeries{Start: when(v.Series.Start), Width: durationpb.New(v.Series.Width)},
	}
	if v.MaxCostUSD > 0 {
		out.MaxCostUsd = &v.MaxCostUSD
	}
	for _, r := range v.Roles {
		out.Roles = append(out.Roles, &pb.RoleCost{
			Role: roles.out(r.Role), Agents: r.Agents, Turns: r.Turns, UnpricedTurns: r.Unpriced, Usage: costUsageOut(r.Usage),
			TotalTokens: r.Tokens,
		})
	}
	kept := map[cost.ModelKey]bool{}
	for _, m := range v.Models {
		if len(kept) == MaxCostModels {
			out.ModelsTruncated = true
			break
		}
		kept[m.Model] = true
		out.Models = append(out.Models, &pb.ModelCost{
			Model: modelRefOut(m.Model, &out.ModelsTruncated), Turns: m.Turns, UnpricedTurns: m.Unpriced,
			Usage: costUsageOut(m.Usage), TotalTokens: m.Tokens,
		})
	}
	for _, b := range v.Series.Buckets {
		bucket := &pb.SpendBucket{Start: when(b.Start), CostUsd: b.Cost}
		for _, m := range b.ByModel {
			if kept[m.Model] {
				bucket.ByModel = append(bucket.ByModel, &pb.ModelSpend{Model: modelRefOut(m.Model, &out.ModelsTruncated), CostUsd: m.Cost})
			}
		}
		out.Series.Buckets = append(out.Series.Buckets, bucket)
	}
	return out
}

// modelRefOut is the contract's message for a model. A name over the limit is cut at a character
// boundary, and cut says so.
func modelRefOut(m cost.ModelKey, cut *bool) *pb.ModelRef {
	name := string(m.Model)
	if len(name) > MaxModelNameBytes {
		n := MaxModelNameBytes
		for n > 0 && !utf8.RuneStart(name[n]) {
			n--
		}
		name, *cut = name[:n], true
	}
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
