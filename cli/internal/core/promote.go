package core

import "time"

// PromoteRefusalKind is one reason a promotion was refused.
type PromoteRefusalKind string

// The refusal kinds.
const (
	RefusalUnmeasured          PromoteRefusalKind = "unmeasured"
	RefusalCheckMissing        PromoteRefusalKind = "check_missing"
	RefusalWorse               PromoteRefusalKind = "worse"
	RefusalWithinNoise         PromoteRefusalKind = "within_noise"
	RefusalInconclusive        PromoteRefusalKind = "inconclusive"
	RefusalStale               PromoteRefusalKind = "stale"
	RefusalEnvironmentMismatch PromoteRefusalKind = "environment_mismatch"
)

// PromoteRefusalReason is one reason with the evidence it rests on.
type PromoteRefusalReason struct {
	Kind    PromoteRefusalKind
	Message string
	// Verdict is set for worse, within_noise and inconclusive.
	Verdict *Verdict
	// MeasuredAgainst and CurrentHead are set for stale.
	MeasuredAgainst Commit
	CurrentHead     Commit
}

// PromoteRefusal is a normal answer to a promotion, not an error. It can carry several reasons.
type PromoteRefusal struct {
	Reasons []PromoteRefusalReason
}

// Promotion is a move of the result branch that happened.
type Promotion struct {
	Commit Commit
	// PreviousHead is empty for the first promotion.
	PreviousHead Commit
	Verdict      Verdict
	Environment  Fingerprint
	// ResultJob is the measurement the promotion rests on.
	ResultJob JobID
	At        time.Time
}
