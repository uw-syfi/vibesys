package main

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Exit codes. Every command ends in one of them, and which one is a function of one answer: the
// command's own (yes or no), a wait's outcome, or a failure's kind. The functions below are the
// whole table; the README prints it, and TestTheReadmeExitTableIsTheCode holds the two together.
const (
	// exitOK is yes: the command did what it was asked, and a wait's jobs succeeded.
	exitOK = 0
	// exitFailed is the daemon's answer no: a job did not succeed, a promotion was refused, there
	// is no current result, or the request conflicts with what is there.
	exitFailed = 1
	// exitInvalidInput is a mistake in the input, a thing named that does not exist included, so a
	// caller can tell a mistyped command from a failed job.
	exitInvalidInput = 2
	// exitTimeout is a wait whose timeout passed first, as the daemon said. No job was cancelled.
	exitTimeout = 3
	// exitNoAnswer is a command that got no answer: no daemon, one it cannot talk to, an internal
	// failure, a wait that could not learn how its jobs ended, or an answer this build cannot read.
	// It says nothing about the jobs: they may still succeed.
	exitNoAnswer = 4
)

// exitOfFailure is the exit code of a command that failed with the kind. A kind this build does
// not know, from a newer daemon, is no answer, never success.
func exitOfFailure(kind cliv1.FailureKind) int {
	switch kind {
	case cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT, cliv1.FailureKind_FAILURE_KIND_NOT_FOUND:
		return exitInvalidInput
	case cliv1.FailureKind_FAILURE_KIND_CONFLICT, cliv1.FailureKind_FAILURE_KIND_ENVIRONMENT_MISMATCH,
		cliv1.FailureKind_FAILURE_KIND_NOT_MEASURED:
		return exitFailed
	case cliv1.FailureKind_FAILURE_KIND_UNSPECIFIED, cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE,
		cliv1.FailureKind_FAILURE_KIND_INCOMPATIBLE, cliv1.FailureKind_FAILURE_KIND_INTERNAL:
		return exitNoAnswer
	}
	// No default above, so archcheck holds the switch to every kind the contract names.
	return exitNoAnswer
}

// exitOfWait is the exit code of a wait that ended with the outcome. An outcome this build does
// not know, from a newer daemon, is no answer, never success.
func exitOfWait(kind cliv1.WaitOutcomeKind) int {
	switch kind {
	case cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED, cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_SUCCEEDED:
		return exitOK
	case cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_SOME_FAILED, cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_FAILED:
		return exitFailed
	case cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_TIMED_OUT:
		return exitTimeout
	case cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_UNSPECIFIED:
		return exitNoAnswer
	}
	// No default above, so archcheck holds the switch to every outcome the contract names.
	return exitNoAnswer
}

// exitOfAnswer is the exit code of a command whose answer is yes or no, such as a promotion that
// was made or refused.
func exitOfAnswer(yes bool) int {
	if yes {
		return exitOK
	}
	return exitFailed
}
