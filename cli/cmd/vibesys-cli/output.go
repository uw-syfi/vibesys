package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// printJSON writes the message as one line of JSON. protojson varies its spacing from build to
// build on purpose, so the output is compacted: the same message is always the same bytes.
func printJSON(d deps, m proto.Message) {
	raw, err := protojson.MarshalOptions{UseProtoNames: true}.Marshal(m)
	if err != nil {
		fmt.Fprintf(d.stderr, "vibesys-cli: encoding the result: %v\n", err)
		return
	}
	var line bytes.Buffer
	if err := json.Compact(&line, raw); err != nil {
		fmt.Fprintf(d.stderr, "vibesys-cli: encoding the result: %v\n", err)
		return
	}
	line.WriteByte('\n')
	_, _ = d.stdout.Write(line.Bytes())
}

// describe is the one line a job takes in human output: its id and state, and the facts of how it
// ended, then the commit and the command.
func describe(j *cliv1.Job) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s\t%s", j.GetId(), stateName(j.GetState()))
	if j.ExitCode != nil {
		fmt.Fprintf(&b, " (exit %d)", j.GetExitCode())
	}
	if j.GetState() == cliv1.JobState_JOB_STATE_CANCELLED && j.GetCancelReason() != "" {
		fmt.Fprintf(&b, " (%s)", j.GetCancelReason())
	}
	commit := j.GetSpec().GetCommit()
	if len(commit) > 12 {
		commit = commit[:12]
	}
	fmt.Fprintf(&b, "\t%s", commit)
	if c := j.GetSpec().GetCommand(); c != nil {
		fmt.Fprintf(&b, "\t%s", strings.Join(c.GetArgv(), " "))
	}
	for _, n := range j.GetNotes() {
		fmt.Fprintf(&b, "\n\t  note: %s", n.GetText())
	}
	return b.String()
}

// fail prints the error of a command and returns the exit code it stands for: 2 for a mistake in
// the input and 1 for anything else. With --json the error is always a Failure.
func fail(d deps, asJSON bool, err error) int {
	failure := &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_INTERNAL, Message: err.Error()}
	var usage usageError
	var away *unavailable
	switch {
	case errors.As(err, &usage):
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT, Message: usage.message}
	case errors.As(err, &away):
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: away.message}
	default:
		if f, ok := wire.FailureOf(err); ok {
			failure = wire.Failure(f)
		}
	}
	if asJSON {
		printJSONTo(d, failure)
	} else {
		fmt.Fprintf(d.stderr, "vibesys-cli: %s\n", failure.GetMessage())
	}
	if failure.GetKind() == cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT {
		return exitInvalidInput
	}
	return exitFailed
}

// failOnJob is fail for a command that names a job: a job that does not exist is a mistake in the
// input.
func failOnJob(d deps, asJSON bool, err error) int {
	code := fail(d, asJSON, err)
	if wire.IsNotFound(err) {
		return exitInvalidInput
	}
	return code
}

// printJSONTo writes a failure to stderr, where errors go, in the same form.
func printJSONTo(d deps, m proto.Message) {
	out := d
	out.stdout = d.stderr
	printJSON(out, m)
}
