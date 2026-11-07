package core

// PlanLink is what a submission says about the plan the job tests: a plan (--plan N), none
// (--no-plan), or nothing. It cannot say both. The zero value says nothing.
//
// A measure job is a point on a run's chart, and the plan it tests is what the app shows next to
// the point. A caller that names a plan or says there is none makes every point exact; a caller
// that says nothing leaves a point the app cannot place.
type PlanLink struct {
	plan PlanNumber
	none bool
}

// NewPlanLink is the link a caller's two statements make: plan is --plan (zero when not given) and
// noPlan is --no-plan. Giving both is refused, naming both flags, since a job tests a plan or none.
// The error is a *Failure of kind FailureInvalidInput.
func NewPlanLink(plan PlanNumber, noPlan bool) (PlanLink, error) {
	if plan != 0 && noPlan {
		return PlanLink{}, failf(FailureInvalidInput,
			"--plan and --no-plan: give one; --plan N links the job to plan N and --no-plan says it tests no plan")
	}
	return PlanLink{plan: plan, none: noPlan}, nil
}

// Plan is the plan the job is linked to, or zero when the caller named none or said nothing.
func (l PlanLink) Plan() PlanNumber { return l.plan }

// Said reports whether the caller named a plan or said there is none.
func (l PlanLink) Said() bool { return l.plan != 0 || l.none }

// Check says whether a job of the kind may be submitted with the link, and the error is a *Failure
// of kind FailureInvalidInput that names the flag at fault. Whether the plan exists is the store's
// to say, so it stays atomic with the write.
//
//   - Only a measure job takes --no-plan: a check or command job is linked to a plan or to none
//     without saying so.
func (l PlanLink) Check(kind JobKind) error {
	if l.none && kind != JobMeasure {
		return failf(FailureInvalidInput, "--no-plan: only a measure job says it tests no plan; a %s job takes --plan N or neither", kind)
	}
	return nil
}
