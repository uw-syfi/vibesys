package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The enumerations a stored record carries beyond those of enums.go. A record keeps whatever the
// domain held, so for these the domain's zero value, the empty string, is the contract's
// unspecified value in both directions: a verdict without a kind is one the domain can hold. As
// with the encoders of enums.go, a value the table does not name encodes as unspecified; the
// decoders refuse a number the table does not name, naming field.

func toEnum[D ~string, W ~int32](field string, table map[D]W, value W) (D, error) {
	if value == 0 {
		return "", nil
	}
	for d, w := range table {
		if w == value {
			return d, nil
		}
	}
	return "", invalid("%s: %d is not a known value", field, int32(value))
}

// VerdictKind returns the contract's value for a verdict kind.
func VerdictKind(k core.VerdictKind) cliv1.VerdictKind { return verdictKinds[k] }

// ToVerdictKind returns the domain kind for the contract's value.
func ToVerdictKind(field string, k cliv1.VerdictKind) (core.VerdictKind, error) {
	return toEnum(field, verdictKinds, k)
}

// InconclusiveReason returns the contract's value for a reason.
func InconclusiveReason(r core.InconclusiveReason) cliv1.InconclusiveReason {
	return inconclusiveReasons[r]
}

// ToInconclusiveReason returns the domain reason for the contract's value.
func ToInconclusiveReason(field string, r cliv1.InconclusiveReason) (core.InconclusiveReason, error) {
	return toEnum(field, inconclusiveReasons, r)
}

// PromoteRefusalKind returns the contract's value for a refusal kind.
func PromoteRefusalKind(k core.PromoteRefusalKind) cliv1.PromoteRefusalKind {
	return refusalKinds[k]
}

// ToPromoteRefusalKind returns the domain kind for the contract's value.
func ToPromoteRefusalKind(field string, k cliv1.PromoteRefusalKind) (core.PromoteRefusalKind, error) {
	return toEnum(field, refusalKinds, k)
}

// Direction returns the contract's value for a metric direction.
func Direction(d core.Direction) cliv1.Direction { return direction(d) }

// ToDirection returns the domain direction for the contract's value.
func ToDirection(field string, d cliv1.Direction) (core.Direction, error) {
	if d == cliv1.Direction_DIRECTION_UNSPECIFIED {
		return "", nil
	}
	return toDirection(field, d)
}

var commands = map[core.Command]cliv1.Command{
	core.CommandSubmitCheck:   cliv1.Command_COMMAND_SUBMIT_CHECK,
	core.CommandSubmitCommand: cliv1.Command_COMMAND_SUBMIT_COMMAND,
	core.CommandSubmitMeasure: cliv1.Command_COMMAND_SUBMIT_MEASURE,
	core.CommandWait:          cliv1.Command_COMMAND_WAIT,
	core.CommandCancel:        cliv1.Command_COMMAND_CANCEL,
	core.CommandNote:          cliv1.Command_COMMAND_NOTE,
	core.CommandJobs:          cliv1.Command_COMMAND_JOBS,
	core.CommandShow:          cliv1.Command_COMMAND_SHOW,
	core.CommandLogs:          cliv1.Command_COMMAND_LOGS,
	core.CommandCompare:       cliv1.Command_COMMAND_COMPARE,
	core.CommandPromote:       cliv1.Command_COMMAND_PROMOTE,
	core.CommandResult:        cliv1.Command_COMMAND_RESULT,
	core.CommandPlanCreate:    cliv1.Command_COMMAND_PLAN_CREATE,
	core.CommandPlanList:      cliv1.Command_COMMAND_PLAN_LIST,
	core.CommandPlanView:      cliv1.Command_COMMAND_PLAN_VIEW,
	core.CommandPlanEdit:      cliv1.Command_COMMAND_PLAN_EDIT,
	core.CommandPlanComment:   cliv1.Command_COMMAND_PLAN_COMMENT,
	core.CommandPlanClose:     cliv1.Command_COMMAND_PLAN_CLOSE,
	core.CommandDaemon:        cliv1.Command_COMMAND_DAEMON,
}

// Command returns the contract's value for a command.
func Command(c core.Command) cliv1.Command { return commands[c] }

// ToCommand returns the domain command for the contract's value.
func ToCommand(field string, c cliv1.Command) (core.Command, error) {
	return toEnum(field, commands, c)
}

var commandOutcomes = map[core.CommandOutcome]cliv1.CommandOutcome{
	core.OutcomeSucceeded: cliv1.CommandOutcome_COMMAND_OUTCOME_SUCCEEDED,
	core.OutcomeFailed:    cliv1.CommandOutcome_COMMAND_OUTCOME_FAILED,
	core.OutcomeRefused:   cliv1.CommandOutcome_COMMAND_OUTCOME_REFUSED,
}

// CommandOutcome returns the contract's value for how a command ended.
func CommandOutcome(o core.CommandOutcome) cliv1.CommandOutcome { return commandOutcomes[o] }

// ToCommandOutcome returns the domain outcome for the contract's value.
func ToCommandOutcome(field string, o cliv1.CommandOutcome) (core.CommandOutcome, error) {
	return toEnum(field, commandOutcomes, o)
}
