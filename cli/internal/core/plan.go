package core

import "time"

// PlanStatus is where a plan stands. Agents choose it: no rule orders the statuses.
type PlanStatus string

// The plan statuses.
const (
	PlanOpen     PlanStatus = "open"
	PlanChosen   PlanStatus = "chosen"
	PlanWon      PlanStatus = "won"
	PlanParked   PlanStatus = "parked"
	PlanRejected PlanStatus = "rejected"
)

// PlanComment is a comment on a plan. Comments carry decisions and results.
type PlanComment struct {
	Text  string
	At    time.Time
	Agent string
}

// Plan is stored the way an issue is: a number the store assigns, a free-form markdown body and
// comments.
type Plan struct {
	Number    PlanNumber
	Title     string
	Body      string
	Status    PlanStatus
	Comments  []PlanComment
	CreatedAt time.Time
	UpdatedAt time.Time
}

// NewPlan is a request to create a plan. The store assigns the number and the open status.
type NewPlan struct {
	Title string
	Body  string
	At    time.Time
}

// PlanEdit changes a plan. A nil field is left as it is, and the whole edit is one change.
type PlanEdit struct {
	Title   *string
	Body    *string
	Status  *PlanStatus
	Comment *PlanComment
	At      time.Time
}

// PlanFilter selects plans. Fields combine with AND and the zero value matches every plan.
type PlanFilter struct {
	Statuses []PlanStatus
	// Text matches a plan whose title or body contains it.
	Text string
}
