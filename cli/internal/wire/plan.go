package wire

import (
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// planStatuses pairs each domain status with its contract value. Both directions read this table,
// so they cannot disagree.
var planStatuses = []struct {
	domain core.PlanStatus
	wire   cliv1.PlanStatus
}{
	{core.PlanOpen, cliv1.PlanStatus_PLAN_STATUS_OPEN},
	{core.PlanChosen, cliv1.PlanStatus_PLAN_STATUS_CHOSEN},
	{core.PlanWon, cliv1.PlanStatus_PLAN_STATUS_WON},
	{core.PlanParked, cliv1.PlanStatus_PLAN_STATUS_PARKED},
	{core.PlanRejected, cliv1.PlanStatus_PLAN_STATUS_REJECTED},
}

// PlanStatus returns the contract's value for a status, or unspecified for a status it does not
// name.
func PlanStatus(s core.PlanStatus) cliv1.PlanStatus {
	for _, p := range planStatuses {
		if p.domain == s {
			return p.wire
		}
	}
	return cliv1.PlanStatus_PLAN_STATUS_UNSPECIFIED
}

// ToPlanStatus returns the domain status for the contract's value, or invalid_input naming field.
func ToPlanStatus(field string, s cliv1.PlanStatus) (core.PlanStatus, error) {
	for _, p := range planStatuses {
		if p.wire == s {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a plan status", field, s)
}

// gainReferences pairs each domain reference with its contract value.
var gainReferences = []struct {
	domain core.GainReference
	wire   cliv1.PlanGainReference
}{
	{core.GainHeadline, cliv1.PlanGainReference_PLAN_GAIN_REFERENCE_HEADLINE},
}

// GainBound returns the contract's message for a bound, nil for none.
func GainBound(b *core.GainBound) *cliv1.PlanGainBound {
	if b == nil {
		return nil
	}
	out := &cliv1.PlanGainBound{Share: b.Share}
	for _, r := range gainReferences {
		if r.domain == b.RelativeTo {
			out.RelativeTo = r.wire
		}
	}
	return out
}

// ToGainBound returns the domain bound for the contract's message, nil for none, or invalid_input
// naming field.
func ToGainBound(field string, b *cliv1.PlanGainBound) (*core.GainBound, error) {
	if b == nil {
		return nil, nil
	}
	for _, r := range gainReferences {
		if r.wire == b.GetRelativeTo() {
			out := core.GainBound{Share: b.GetShare(), RelativeTo: r.domain}
			if err := core.CheckGainBound(field, out); err != nil {
				return nil, err
			}
			return &out, nil
		}
	}
	return nil, invalid("%s.relative_to: %s is not something a bound is a share of", field, b.GetRelativeTo())
}

// GainCheck returns the contract's message for the answer to a gain check.
func GainCheck(c core.GainCheck) *cliv1.PlanGainCheck {
	answer := cliv1.PlanGainAnswer_PLAN_GAIN_ANSWER_NOT_ALL_UNDER
	if c.AllUnder() {
		answer = cliv1.PlanGainAnswer_PLAN_GAIN_ANSWER_ALL_UNDER
	}
	out := &cliv1.PlanGainCheck{Share: c.Share, Answer: answer, Counted: uint32(c.Counted)}
	for _, p := range c.Blockers {
		out.Blockers = append(out.Blockers, &cliv1.PlanGainBlocker{
			Number: uint32(p.Number), Title: p.Title, Status: PlanStatus(p.Status), GainBound: GainBound(p.GainBound),
		})
	}
	return out
}

// PlanComment returns the contract's message for a comment.
func PlanComment(c core.PlanComment) *cliv1.PlanComment {
	return &cliv1.PlanComment{Text: c.Text, At: timestamp(c.At), Agent: c.Agent}
}

// ToPlanComment returns the domain comment for the contract's message.
func ToPlanComment(c *cliv1.PlanComment) core.PlanComment {
	return core.PlanComment{Text: c.GetText(), At: toTime(c.GetAt()), Agent: c.GetAgent()}
}

// PlanTransition returns the contract's message for a transition.
func PlanTransition(t core.PlanTransition) *cliv1.PlanTransition {
	return &cliv1.PlanTransition{Status: PlanStatus(t.Status), Origin: RecordOrigin(t.Origin)}
}

// Plan returns the contract's message for a plan.
func Plan(p core.Plan) *cliv1.Plan {
	out := &cliv1.Plan{
		Number:     uint32(p.Number),
		Title:      p.Title,
		Body:       p.Body,
		Status:     PlanStatus(p.Status),
		CreatedAt:  timestamp(p.CreatedAt),
		UpdatedAt:  timestamp(p.UpdatedAt),
		GainBound:  GainBound(p.GainBound),
		Workstream: uint32(p.Workstream),
	}
	if p.Origin != nil {
		out.Origin = RecordOrigin(*p.Origin)
	}
	for _, c := range p.Comments {
		out.Comments = append(out.Comments, PlanComment(c))
	}
	for _, t := range p.Transitions {
		out.Transitions = append(out.Transitions, PlanTransition(t))
	}
	return out
}

// Plans returns the contract's messages for plans, in order.
func Plans(plans []core.Plan) []*cliv1.Plan {
	out := make([]*cliv1.Plan, 0, len(plans))
	for _, p := range plans {
		out = append(out, Plan(p))
	}
	return out
}

// ToPlan returns the domain plan for the contract's message.
func ToPlan(p *cliv1.Plan) (core.Plan, error) {
	status, err := ToPlanStatus("plan.status", p.GetStatus())
	if err != nil {
		return core.Plan{}, err
	}
	bound, err := ToGainBound("plan.gain_bound", p.GetGainBound())
	if err != nil {
		return core.Plan{}, err
	}
	out := core.Plan{
		Number: core.PlanNumber(p.GetNumber()), Title: p.GetTitle(), Body: p.GetBody(), Status: status,
		CreatedAt: toTime(p.GetCreatedAt()), UpdatedAt: toTime(p.GetUpdatedAt()), GainBound: bound,
		Workstream: core.WorkstreamNumber(p.GetWorkstream()),
	}
	if p.GetOrigin() != nil {
		origin := toRecordOrigin(p.GetOrigin())
		out.Origin = &origin
	}
	for _, c := range p.GetComments() {
		out.Comments = append(out.Comments, ToPlanComment(c))
	}
	for i, t := range p.GetTransitions() {
		ts, err := ToPlanStatus(fmt.Sprintf("plan.transitions[%d].status", i), t.GetStatus())
		if err != nil {
			return core.Plan{}, err
		}
		out.Transitions = append(out.Transitions, core.PlanTransition{Status: ts, Origin: toRecordOrigin(t.GetOrigin())})
	}
	return out, nil
}

// PlanView returns the contract's message for a plan and the jobs linked to it, each with its
// commit's verdict against the result branch's head when the view has one.
func PlanView(v core.PlanView) *cliv1.PlanView {
	out := &cliv1.PlanView{Plan: Plan(v.Plan)}
	for _, j := range v.Jobs {
		linked := &cliv1.LinkedJob{Job: Job(j)}
		if verdict, ok := v.Verdicts[j.Spec.Commit]; ok {
			linked.Verdict = Verdict(verdict)
		}
		out.Jobs = append(out.Jobs, linked)
	}
	return out
}

// ToPlanFilter returns the domain filter for a ListPlans request.
func ToPlanFilter(r *cliv1.ListPlansRequest) (core.PlanFilter, error) {
	filter := core.PlanFilter{Text: r.GetText()}
	for _, s := range r.GetStatuses() {
		status, err := ToPlanStatus("statuses", s)
		if err != nil {
			return core.PlanFilter{}, err
		}
		filter.Statuses = append(filter.Statuses, status)
	}
	return filter, nil
}

// CheckGain answers a gain check over plans as the contract has them: which of the open or parked
// ones state a bound below share. A plan the contract does not allow is invalid input.
func CheckGain(plans []*cliv1.Plan, share float64) (*cliv1.PlanGainCheck, error) {
	domain := make([]core.Plan, 0, len(plans))
	for _, p := range plans {
		d, err := ToPlan(p)
		if err != nil {
			return nil, err
		}
		domain = append(domain, d)
	}
	return GainCheck(core.CheckGain(domain, share)), nil
}
