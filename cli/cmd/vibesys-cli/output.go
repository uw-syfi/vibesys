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
	switch spec := j.GetSpec(); {
	case spec.GetCommand() != nil:
		fmt.Fprintf(&b, "\t[%s] %s", imageName(spec.GetCommand()), commandSummary(spec.GetCommand().GetArgv()))
	case spec.GetCheck() != nil:
		fmt.Fprint(&b, "\tcheck")
	case spec.GetMeasure() != nil:
		fmt.Fprintf(&b, "\tmeasure (%d samples)", spec.GetMeasure().GetSamples())
	}
	if p := placementOf(j.GetEnvironment()); p != "" {
		fmt.Fprintf(&b, "\ton %s", p)
	}
	for _, n := range j.GetNotes() {
		fmt.Fprintf(&b, "\n\t  note: %s", n.GetText())
	}
	return b.String()
}

// imageName is the image a command job runs in, as --image names it. A job stored before the image
// was named has none, and its candidate setup says where it ran.
func imageName(c *cliv1.CommandSettings) string {
	switch {
	case c.GetImage() == cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE:
		return "candidate"
	case c.GetImage() == cliv1.CommandImage_COMMAND_IMAGE_UNSPECIFIED && c.GetCandidate() != nil && c.GetCandidate().GetServe() == nil:
		return "candidate"
	}
	return "harness"
}

// commandWidth is the most runes of a command's argv that describe prints.
const commandWidth = 120

// commandSummary is a command's argv as describe prints it: on one line and at most commandWidth
// runes, so an inline script passed as an argument does not fill every listing that names its
// job. What it leaves out it says, and --json carries the whole argv.
func commandSummary(argv []string) string {
	joined := strings.Join(argv, " ")
	first, rest, multiline := strings.Cut(joined, "\n")
	runes := []rune(first)
	if !multiline && len(runes) <= commandWidth {
		return joined
	}
	if len(runes) > commandWidth {
		first = string(runes[:commandWidth])
	}
	more := ""
	if multiline {
		more = fmt.Sprintf("%d more lines, ", strings.Count(rest, "\n")+1)
	}
	return strings.TrimRight(first, " \t\r") + " … (" + more + "full argv in --json)"
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

// fail prints the error of a command and returns its exit code, from the failure's kind
// (exitOfFailure). With --json the error is always a Failure: the one the daemon sent, exactly,
// with a kind this build may not know; or the one the command made itself, for a mistake in its
// input or a daemon it could not reach.
func fail(d deps, asJSON bool, err error) int {
	var usage usageError
	var away *unavailable
	var failure *cliv1.Failure
	switch {
	case errors.As(err, &usage):
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT, Message: usage.message}
	case errors.As(err, &away):
		failure = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: away.message}
	default:
		f, ok := wire.ContractFailure(err)
		if !ok {
			// The caller's own cancel, such as a signal: the command has no answer to give.
			f = &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: err.Error()}
		}
		failure = f
	}
	if asJSON {
		printJSONTo(d, failure)
	} else {
		fmt.Fprintf(d.stderr, "vibesys-cli: %s\n", failure.GetMessage())
	}
	return exitOfFailure(failure.GetKind())
}

// printJSONTo writes a failure to stderr, where errors go, in the same form.
func printJSONTo(d deps, m proto.Message) {
	out := d
	out.stdout = d.stderr
	printJSON(out, m)
}
