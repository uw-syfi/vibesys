package main

import (
	"bytes"
	"encoding/json"
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
	switch spec := j.GetSpec(); {
	case spec.GetCommand() != nil:
		fmt.Fprintf(&b, "\t%s", strings.Join(spec.GetCommand().GetArgv(), " "))
	case spec.GetCheck() != nil:
		fmt.Fprint(&b, "\tcheck")
	case spec.GetMeasure() != nil:
		fmt.Fprintf(&b, "\tmeasure (%d samples)", spec.GetMeasure().GetSamples())
	}
	for _, n := range j.GetNotes() {
		fmt.Fprintf(&b, "\n\t  note: %s", n.GetText())
	}
	return b.String()
}

// describeSubmitted is describe, with what the submission turned into when it did not make a new
// job: a job that was joined or reused ran, or runs, for an earlier submission.
func describeSubmitted(r *cliv1.SubmitResponse) string {
	line := describe(r.GetJob())
	switch r.GetDisposition() {
	case cliv1.SubmitDisposition_SUBMIT_DISPOSITION_JOINED:
		line += "\t(joined an existing job)"
	case cliv1.SubmitDisposition_SUBMIT_DISPOSITION_REUSED:
		line += "\t(reused a saved result)"
	}
	return line
}

// fail prints the error of a command and returns the exit code it stands for: 2 for a mistake in
// the input, 1 for anything else.
func fail(d deps, asJSON bool, err error) int {
	code := exitFailed
	var message string
	var failure *cliv1.Failure
	switch e := err.(type) {
	case usageError:
		code = exitInvalidInput
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT, Message: e.message}
	case *unavailable:
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: e.message}
	default:
		if f, ok := wire.FailureOf(err); ok {
			failure = wire.Failure(f)
			if failure.GetKind() == cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT {
				code = exitInvalidInput
			}
		}
	}
	switch {
	case asJSON && failure != nil:
		printJSONTo(d, failure)
	case failure != nil:
		message = failure.GetMessage()
		fmt.Fprintf(d.stderr, "vibesys-cli: %s\n", message)
	default:
		fmt.Fprintf(d.stderr, "vibesys-cli: %v\n", err)
	}
	return code
}

// printJSONTo writes a failure to stderr, where errors go, in the same form.
func printJSONTo(d deps, m proto.Message) {
	out := d
	out.stdout = d.stderr
	printJSON(out, m)
}
