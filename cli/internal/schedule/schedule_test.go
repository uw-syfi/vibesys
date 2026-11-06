package schedule_test

import (
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// optionOf is the option of a waiting job that claims the resource, or its first.
func optionOf(s schedule.State, id schedule.ID, r schedule.ResourceID) schedule.Option {
	i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == id })
	for _, o := range s.Waiting[i].Options {
		if slices.ContainsFunc(o.Claims, func(c schedule.Claim) bool { return c.Resource == r }) {
			return o
		}
	}
	return s.Waiting[i].Options[0]
}

type reversed struct{}

func (reversed) Rank(options []schedule.Option, load schedule.Load) []schedule.Option {
	out := schedule.LeastLoaded{}.Rank(options, load)
	slices.Reverse(out)
	return out
}

type dropping struct{}

func (dropping) Rank([]schedule.Option, schedule.Load) []schedule.Option { return nil }

type inventing struct{}

func (inventing) Rank(options []schedule.Option, _ schedule.Load) []schedule.Option {
	made := schedule.Option{Placement: "made-up", Claims: []schedule.Claim{{Resource: "nothing", Amount: 1}}}
	return append([]schedule.Option{made}, options...)
}

func mapDecisions(ds []schedule.Decision, f func(schedule.Decision) schedule.Decision) []schedule.Decision {
	out := make([]schedule.Decision, len(ds))
	for i, d := range ds {
		out[i] = f(d)
	}
	return out
}
