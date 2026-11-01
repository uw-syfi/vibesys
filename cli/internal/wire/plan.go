package wire

import (
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

// PlanComment returns the contract's message for a comment.
func PlanComment(c core.PlanComment) *cliv1.PlanComment {
	return &cliv1.PlanComment{Text: c.Text, At: timestamp(c.At), Agent: c.Agent}
}

// ToPlanComment returns the domain comment for the contract's message.
func ToPlanComment(c *cliv1.PlanComment) core.PlanComment {
	return core.PlanComment{Text: c.GetText(), At: toTime(c.GetAt()), Agent: c.GetAgent()}
}

// Plan returns the contract's message for a plan.
func Plan(p core.Plan) *cliv1.Plan {
	out := &cliv1.Plan{
		Number:    uint32(p.Number),
		Title:     p.Title,
		Body:      p.Body,
		Status:    PlanStatus(p.Status),
		CreatedAt: timestamp(p.CreatedAt),
		UpdatedAt: timestamp(p.UpdatedAt),
	}
	for _, c := range p.Comments {
		out.Comments = append(out.Comments, PlanComment(c))
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
	out := core.Plan{
		Number: core.PlanNumber(p.GetNumber()), Title: p.GetTitle(), Body: p.GetBody(), Status: status,
		CreatedAt: toTime(p.GetCreatedAt()), UpdatedAt: toTime(p.GetUpdatedAt()),
	}
	for _, c := range p.GetComments() {
		out.Comments = append(out.Comments, ToPlanComment(c))
	}
	return out, nil
}

// PlanView returns the contract's message for a plan and the jobs linked to it. The linked jobs'
// verdicts are not filled in yet: the comparison they come from is not available in this build.
func PlanView(v core.PlanView) *cliv1.PlanView {
	out := &cliv1.PlanView{Plan: Plan(v.Plan)}
	for _, j := range v.Jobs {
		out.Jobs = append(out.Jobs, &cliv1.LinkedJob{Job: Job(j)})
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
