package wire

import (
	"fmt"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func invalid(format string, args ...any) *core.Failure {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// jobStates pairs each domain state with its contract value. Both directions read this table, so
// they cannot disagree.
var jobStates = []struct {
	domain core.JobState
	wire   cliv1.JobState
}{
	{core.JobQueued, cliv1.JobState_JOB_STATE_QUEUED},
	{core.JobRunning, cliv1.JobState_JOB_STATE_RUNNING},
	{core.JobSucceeded, cliv1.JobState_JOB_STATE_SUCCEEDED},
	{core.JobFailed, cliv1.JobState_JOB_STATE_FAILED},
	{core.JobCancelled, cliv1.JobState_JOB_STATE_CANCELLED},
	{core.JobLost, cliv1.JobState_JOB_STATE_LOST},
}

// JobState returns the contract's value for a state, or unspecified for a state it does not name.
func JobState(s core.JobState) cliv1.JobState {
	for _, p := range jobStates {
		if p.domain == s {
			return p.wire
		}
	}
	return cliv1.JobState_JOB_STATE_UNSPECIFIED
}

// ToJobState returns the domain state for the contract's value, or invalid_input naming field.
func ToJobState(field string, s cliv1.JobState) (core.JobState, error) {
	for _, p := range jobStates {
		if p.wire == s {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a job state", field, s)
}

var jobKinds = []struct {
	domain core.JobKind
	wire   cliv1.JobKind
}{
	{core.JobCheck, cliv1.JobKind_JOB_KIND_CHECK},
	{core.JobCommand, cliv1.JobKind_JOB_KIND_COMMAND},
	{core.JobMeasure, cliv1.JobKind_JOB_KIND_MEASURE},
}

// JobKind returns the contract's value for a kind.
func JobKind(k core.JobKind) cliv1.JobKind {
	for _, p := range jobKinds {
		if p.domain == k {
			return p.wire
		}
	}
	return cliv1.JobKind_JOB_KIND_UNSPECIFIED
}

// ToJobKind returns the domain kind for the contract's value. Unspecified is the empty kind,
// which a filter reads as every kind.
func ToJobKind(field string, k cliv1.JobKind) (core.JobKind, error) {
	if k == cliv1.JobKind_JOB_KIND_UNSPECIFIED {
		return "", nil
	}
	for _, p := range jobKinds {
		if p.wire == k {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a job kind", field, k)
}

var environmentKinds = []struct {
	domain core.EnvironmentKind
	wire   cliv1.EnvironmentKind
}{
	{core.EnvironmentAuto, cliv1.EnvironmentKind_ENVIRONMENT_KIND_UNSPECIFIED},
	{core.EnvironmentHost, cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST},
	{core.EnvironmentDocker, cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER},
}

// EnvironmentKind returns the contract's value for an environment kind. Auto is unspecified.
func EnvironmentKind(k core.EnvironmentKind) cliv1.EnvironmentKind {
	for _, p := range environmentKinds {
		if p.domain == k {
			return p.wire
		}
	}
	return cliv1.EnvironmentKind_ENVIRONMENT_KIND_UNSPECIFIED
}

// ToEnvironmentKind returns the domain kind for the contract's value.
func ToEnvironmentKind(field string, k cliv1.EnvironmentKind) (core.EnvironmentKind, error) {
	for _, p := range environmentKinds {
		if p.wire == k {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not an environment kind", field, k)
}

var backendKinds = []struct {
	domain core.BackendKind
	wire   cliv1.BackendKind
}{
	{core.BackendCPU, cliv1.BackendKind_BACKEND_KIND_CPU},
	{core.BackendCUDA, cliv1.BackendKind_BACKEND_KIND_CUDA},
	{core.BackendROCm, cliv1.BackendKind_BACKEND_KIND_ROCM},
	{core.BackendMetal, cliv1.BackendKind_BACKEND_KIND_METAL},
	{core.BackendTrainium, cliv1.BackendKind_BACKEND_KIND_TRAINIUM},
}

// BackendKind returns the contract's value for a backend.
func BackendKind(k core.BackendKind) cliv1.BackendKind {
	for _, p := range backendKinds {
		if p.domain == k {
			return p.wire
		}
	}
	return cliv1.BackendKind_BACKEND_KIND_UNSPECIFIED
}

// ToBackendKind returns the domain backend for the contract's value.
func ToBackendKind(field string, k cliv1.BackendKind) (core.BackendKind, error) {
	for _, p := range backendKinds {
		if p.wire == k {
			return p.domain, nil
		}
	}
	return "", invalid("%s: %s is not a backend", field, k)
}

var dispositions = []struct {
	domain core.SubmitDisposition
	wire   cliv1.SubmitDisposition
}{
	{core.SubmitCreated, cliv1.SubmitDisposition_SUBMIT_DISPOSITION_CREATED},
	{core.SubmitJoined, cliv1.SubmitDisposition_SUBMIT_DISPOSITION_JOINED},
	{core.SubmitReused, cliv1.SubmitDisposition_SUBMIT_DISPOSITION_REUSED},
}

// SubmitDisposition returns the contract's value for a disposition.
func SubmitDisposition(d core.SubmitDisposition) cliv1.SubmitDisposition {
	for _, p := range dispositions {
		if p.domain == d {
			return p.wire
		}
	}
	return cliv1.SubmitDisposition_SUBMIT_DISPOSITION_UNSPECIFIED
}

var executionKinds = []struct {
	domain core.ExecKind
	wire   cliv1.ExecutionKind
}{
	{core.ExecExited, cliv1.ExecutionKind_EXECUTION_KIND_EXITED},
	{core.ExecTimedOut, cliv1.ExecutionKind_EXECUTION_KIND_TIMED_OUT},
	{core.ExecCancelled, cliv1.ExecutionKind_EXECUTION_KIND_CANCELLED},
	{core.ExecServiceNotReady, cliv1.ExecutionKind_EXECUTION_KIND_SERVICE_NOT_READY},
	{core.ExecServiceExited, cliv1.ExecutionKind_EXECUTION_KIND_SERVICE_EXITED},
	{core.ExecUnknown, cliv1.ExecutionKind_EXECUTION_KIND_UNKNOWN},
}

// ExecutionKind returns the contract's value for how a command ended.
func ExecutionKind(k core.ExecKind) cliv1.ExecutionKind {
	for _, p := range executionKinds {
		if p.domain == k {
			return p.wire
		}
	}
	return cliv1.ExecutionKind_EXECUTION_KIND_UNSPECIFIED
}

// ToExecKind returns how a command ended for the contract's value. A value this build does not
// know, which a supervisor from a newer build may send, is ExecUnknown and never a success.
func ToExecKind(k cliv1.ExecutionKind) core.ExecKind {
	for _, p := range executionKinds {
		if p.wire == k {
			return p.domain
		}
	}
	return core.ExecUnknown
}
