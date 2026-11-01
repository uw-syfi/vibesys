package core

import "fmt"

// FailureKind is why a command could not do what it was asked.
type FailureKind string

// The failure kinds.
const (
	FailureInvalidInput FailureKind = "invalid_input"
	FailureNotFound     FailureKind = "not_found"
	// FailureConflict is a request that conflicts with current state, such as a stale Transition.
	FailureConflict FailureKind = "conflict"
	// FailureUnavailable is something the command needs that is not there, such as Docker.
	FailureUnavailable         FailureKind = "unavailable"
	FailureIncompatible        FailureKind = "incompatible"
	FailureEnvironmentMismatch FailureKind = "environment_mismatch"
	FailureNotMeasured         FailureKind = "not_measured"
	FailureInternal            FailureKind = "internal"
)

// Failure is a typed error. Callers branch on Kind with errors.As; Message is display text.
type Failure struct {
	Kind    FailureKind
	Message string
}

// Error implements error.
func (f *Failure) Error() string { return string(f.Kind) + ": " + f.Message }

// failf builds a Failure of a kind with a formatted message.
func failf(kind FailureKind, format string, args ...any) *Failure {
	return &Failure{Kind: kind, Message: fmt.Sprintf(format, args...)}
}
