package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A frontier only goes out: the daemon sends its decisions and its members to a client, and
// receives nothing but a commit. The store keeps the members in its own records.

var frontierRefusalKinds = map[core.FrontierRefusalKind]cliv1.FrontierRefusalKind{
	core.FrontierUnmeasured:          cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_UNMEASURED,
	core.FrontierCheckMissing:        cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_CHECK_MISSING,
	core.FrontierDominated:           cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_DOMINATED,
	core.FrontierWithinNoise:         cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_WITHIN_NOISE,
	core.FrontierInconclusive:        cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_INCONCLUSIVE,
	core.FrontierStale:               cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_STALE,
	core.FrontierEnvironmentMismatch: cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_ENVIRONMENT_MISMATCH,
}

// FrontierMember returns the contract's message for a member.
func FrontierMember(m core.FrontierMember) *cliv1.FrontierMember {
	return &cliv1.FrontierMember{Commit: string(m.Commit), AddedAt: timestamp(m.AddedAt)}
}

func frontierMembers(ms []core.FrontierMember) []*cliv1.FrontierMember {
	var out []*cliv1.FrontierMember
	for _, m := range ms {
		out = append(out, FrontierMember(m))
	}
	return out
}

// FrontierDecision returns the contract's message for what the frontier made of a commit.
func FrontierDecision(d core.FrontierDecision) *cliv1.FrontierDecision {
	switch d := d.(type) {
	case core.FrontierAdmission:
		var evicted []string
		for _, c := range d.Evicted() {
			evicted = append(evicted, string(c))
		}
		return &cliv1.FrontierDecision{Outcome: &cliv1.FrontierDecision_Admitted{Admitted: &cliv1.FrontierAdmission{
			Added: FrontierMember(d.Added()), Evicted: evicted, AlreadyMember: d.AlreadyMember(),
			Members: frontierMembers(d.Members()),
		}}}
	case core.FrontierRefusal:
		refusal := &cliv1.FrontierRefusal{}
		for _, r := range d.Reasons {
			m := &cliv1.FrontierRefusalReason{
				Kind: frontierRefusalKinds[r.Kind], Message: r.Message, Member: string(r.Member),
				Objectives: ObjectiveVerdicts(r.Objectives),
			}
			if r.Verdict != nil {
				m.Verdict = Verdict(*r.Verdict)
			}
			refusal.Reasons = append(refusal.Reasons, m)
		}
		return &cliv1.FrontierDecision{Outcome: &cliv1.FrontierDecision_Refused{Refused: refusal}}
	}
	return &cliv1.FrontierDecision{}
}

// FrontierEntries returns the contract's messages for the members of a frontier with their
// headline numbers.
func FrontierEntries(entries []core.FrontierEntry) []*cliv1.FrontierEntry {
	out := make([]*cliv1.FrontierEntry, 0, len(entries))
	for _, e := range entries {
		m := &cliv1.FrontierEntry{Member: FrontierMember(e.Member)}
		for _, v := range e.Values {
			m.Values = append(m.Values, &cliv1.ObjectiveValue{
				Metric: &cliv1.Metric{Name: v.Metric.Name, Direction: direction(v.Metric.Direction)},
				Value:  v.Value, Samples: v.Samples,
			})
		}
		out = append(out, m)
	}
	return out
}
