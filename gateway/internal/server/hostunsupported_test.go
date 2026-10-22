package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"google.golang.org/protobuf/proto"
)

// A failed host command, drawn from what hosts write in the field: any exit
// status, any stdout, and a stderr built from argparse's two ways of rejecting
// a command, look-alikes of them, partial evidence and noise. The generator
// labels the evidence it wrote, so the expected category never comes from the
// code under test.
type failedCommand struct {
	cli      string
	exitCode int
	output   []byte
	stderr   string
	start    bool     // the command did not start: the error is not an exit status
	rejected []string // the commands stderr explicitly says the checkout does not know
}

const (
	versioned   = "inspect-candidate"
	propertyRun = 400
)

// otherCommands are host commands that are never versioned, and look-alike
// words for the versioned one.
var otherCommands = []string{"list-examples", "describe-machine", "login", "remove-cluster", "post-intent"}

func (failedCommand) Generate(random *rand.Rand, _ int) reflect.Value {
	clis := append(commandsOf(versionedCommands), otherCommands...)
	failed := failedCommand{
		cli:      pick(random, clis),
		exitCode: pick(random, []int{0, 1, 2, 2, 2, 2, 2, 2, 3, 126, 127, 255, -1}),
		start:    random.Intn(8) == 0,
	}
	if random.Intn(2) == 0 {
		failed.cli = versioned
	}
	if random.Intn(5) == 0 {
		failed.output = []byte{0x0a, byte(random.Intn(256))}
	}
	if failed.exitCode == 0 {
		failed.start = true // a command that exited 0 failed only by not starting
	}
	var lines []string
	for range random.Intn(5) {
		line, rejects := fragment(random, failed.cli)
		lines = append(lines, line)
		if rejects != "" {
			failed.rejected = append(failed.rejected, rejects)
		}
	}
	failed.stderr = strings.Join(lines, "\n")
	return reflect.ValueOf(failed)
}

func commandsOf(set map[string]bool) []string {
	var names []string
	for name := range set {
		names = append(names, name)
	}
	slices.Sort(names)
	return names
}

func pick[T any](random *rand.Rand, values []T) T { return values[random.Intn(len(values))] }

// fragment is one stderr line and the command it explicitly rejects, if any.
func fragment(random *rand.Rand, cli string) (line, rejects string) {
	name := cli
	switch random.Intn(4) {
	case 0:
		name = pick(random, otherCommands)
	case 1:
		name = versioned
	}
	lookalike := pick(random, []string{name + "-v2", "--" + name, "x" + name, "/work/" + name, name + ".py", name + "=1"})
	switch random.Intn(12) {
	case 0, 1:
		return "vibesys: error: unrecognized arguments: " + pick(random, []string{"", "--verbose "}) + "host " + name, name
	case 2, 3:
		return "vibesys host: error: argument command: invalid choice: '" + name + "' (choose from 'list-examples')", name
	case 4:
		return "vibesys: error: unrecognized arguments: host " + lookalike, ""
	case 5:
		return "vibesys host: error: argument command: invalid choice: '" + lookalike + "' (choose from 'list-examples')", ""
	case 6:
		return pick(random, []string{"invalid choice: " + name, "invalid choice: '" + name, "unrecognized arguments:", "invalid choice:"}), ""
	case 7:
		return "usage: vibesys host [-h] {" + name + "} ...", ""
	case 8:
		return "vibesys host: error: argument command: invalid choice: 'x' (choose from '" + name + "')", ""
	case 9:
		return "see " + name + " docs", ""
	case 10:
		return pick(random, []string{"Traceback (most recent call last):", "ModuleNotFoundError: No module named 'vibesys'", "/src/checkout/.venv/bin/python: can't open file '/src/checkout/vibesys.py': [Errno 2] No such file or directory"}), ""
	default:
		return "", ""
	}
}

// Whether the checkout lacks the versioned command: the only failure that
// means "needs an update".
func (f failedCommand) lacksCommand() bool {
	return versionedCommands[f.cli] && f.exitCode == usageExitStatus && len(f.output) == 0 && !f.start && slices.Contains(f.rejected, f.cli)
}

// reported is the stderr a failure carries: a command that did not start wrote none.
func (f failedCommand) reported() string {
	if f.start {
		return ""
	}
	return safeDiagnostic(f.stderr)
}

func (f failedCommand) result() (hostcommand.Result, error) {
	if f.start {
		return hostcommand.Result{}, errors.New("fork failed")
	}
	return hostcommand.Result{ExitCode: f.exitCode, Output: f.output, Stderr: f.stderr}, &exec.ExitError{}
}

// stream is how a streamed command ends without a first event.
func (f failedCommand) stream() error {
	if f.start {
		return errors.New("fork failed")
	}
	return &hostcommand.ExitError{Err: errors.New("exit status"), Code: f.exitCode, Stderr: f.stderr}
}

func check(t *testing.T, property any) {
	t.Helper()
	config := &quick.Config{MaxCount: propertyRun, Rand: rand.New(rand.NewSource(7))}
	if err := quick.Check(property, config); err != nil {
		t.Fatal(err)
	}
}

func TestOnlyExplicitUnknownCommandEvidenceTypesAVersionedCommandAsUnsupported(t *testing.T) {
	check(t, func(f failedCommand) bool {
		result, err := f.result()
		category, message := commandFailure(f.cli, result, err)
		if f.lacksCommand() {
			return category == hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED && message == unsupportedMessage(f.cli)
		}
		return category == hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL &&
			strings.Contains(message, fmt.Sprintf("vibesys host %s exited %d", f.cli, result.ExitCode)) &&
			strings.HasSuffix(message, "\n"+f.reported())
	})
}

func TestAStreamIsUnsupportedOnlyOnTheSameEvidence(t *testing.T) {
	check(t, func(f failedCommand) bool {
		// A stream that sent nothing has no stdout, which the generated case may have.
		silent := f
		silent.output = nil
		return unsupportedStream(f.cli, f.stream()) == silent.lacksCommand()
	})
}

func TestEntryPointsKeepAnOrdinaryFailureItsCategoryAndDiagnostic(t *testing.T) {
	var current failedCommand
	runner := hostcommand.FakeRunner{Program: func(_ context.Context, command hostcommand.Command) (hostcommand.Result, error) {
		if command.Args[1] == "login" {
			return hostcommand.Result{}, current.stream()
		}
		return current.result()
	}}
	root := t.TempDir()
	executable := filepath.Join(root, ".venv", "bin", "vibesys")
	if err := os.MkdirAll(filepath.Dir(executable), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(executable, []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	service := &host{config: Config{Root: t.TempDir(), HostRunner: runner}}
	ctx := context.Background()
	unsupported := hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
	internal := hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL

	check(t, func(f failedCommand) bool {
		current = f

		inspected, err := service.Read(ctx, &apiv1.ReadRequest{
			Checkout: root,
			Method: &apiv1.ReadRequest_InspectCandidate{InspectCandidate: &hostv1.InspectCandidateRequest{
				RequestId: "inspect-a", Path: root, SelectionRevision: 1,
			}},
		})
		current.cli = versioned
		lacks := current.lacksCommand()
		if err != nil || (inspected.GetFailure().GetCategory() == unsupported) != lacks {
			return false
		}
		if !lacks && (inspected.GetFailure().GetCategory() != internal || inspected.GetFailure().GetMessage() != "vibesys host inspect-candidate failed") {
			return false
		}

		// Every other command exits the same way, and none of them is ever unsupported.
		current.cli = ""
		read, err := service.Read(ctx, &apiv1.ReadRequest{
			Checkout: root,
			Method:   &apiv1.ReadRequest_ListExamples{ListExamples: &hostv1.ListExamplesRequest{RequestId: "list-a"}},
		})
		if err != nil || !ordinary(read.GetFailure().GetCategory(), read.GetFailure().GetMessage(), current) {
			return false
		}
		mutate, err := service.Mutate(ctx, &apiv1.MutateRequest{
			Checkout: root,
			Method:   &apiv1.MutateRequest_RemoveCluster{RemoveCluster: &hostv1.RemoveClusterRequest{RequestId: "rm-a", Name: "cluster-a"}},
		})
		if err != nil || !ordinary(mutate.GetFailure().GetCategory(), mutate.GetFailure().GetMessage(), current) {
			return false
		}
		call, err := service.HostCall(ctx, &apiv1.HostCallRequest{
			Checkout: root,
			Method: &apiv1.HostCallRequest_PostIntent{PostIntent: &apiv1.PostIntentRequest{
				ExpectedEpoch: 1, ProjectRoot: "/project", RunId: "run-1", RequestId: "r-1",
				Intent: &apiv1.PostIntentRequest_Pause{Pause: &apiv1.PauseIntent{}},
			}},
		})
		failed := call.GetConfigurationFailed()
		if err != nil || failed.GetCode() == "host_unsupported" || failed.GetStderr() != current.reported() {
			return false
		}
		stream := &watchStream{ctx: ctx}
		request := &apiv1.WatchRequest{
			Checkout: root,
			Method:   &apiv1.WatchRequest_Login{Login: &hostv1.LoginRequest{RequestId: "login-a", Provider: "claude"}},
		}
		if err := service.Watch(request, stream); err != nil || len(stream.sent) != 1 {
			return false
		}
		failure := stream.sent[0].GetFailure()
		return failure.GetCategory() == internal && strings.Contains(failure.GetMessage(), current.reported())
	})
}

// ordinary reports whether a failure of an unversioned command kept its own
// category and its stderr.
func ordinary(category hostv1.FailureCategory, message string, f failedCommand) bool {
	return category == hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL && strings.Contains(message, strings.TrimSpace(f.reported()))
}

func TestTheGeneratorReachesBothSidesOfTheEvidenceBoundary(t *testing.T) {
	random := rand.New(rand.NewSource(7))
	var lacking, nearMisses int
	for range propertyRun {
		f := failedCommand{}.Generate(random, 0).Interface().(failedCommand)
		reachable := versionedCommands[f.cli] && f.exitCode == usageExitStatus && len(f.output) == 0 && !f.start
		switch {
		case f.lacksCommand():
			lacking++
		case reachable && strings.Contains(f.stderr, f.cli):
			nearMisses++
		}
	}
	if lacking < 10 || nearMisses < 10 {
		t.Fatalf("lacking %d, near misses %d of %d", lacking, nearMisses, propertyRun)
	}
}

// hostCommands lists the CLI of every method of one request kind.
func hostCommands[M proto.Message](empty M, dispatch func(M) (hostoperations.HostCall, bool)) []string {
	fields := empty.ProtoReflect().Descriptor().Oneofs().ByName("method").Fields()
	var commands []string
	for i := range fields.Len() {
		request := empty.ProtoReflect().New()
		request.Set(fields.Get(i), request.NewField(fields.Get(i)))
		if call, ok := dispatch(request.Interface().(M)); ok {
			commands = append(commands, call.CLI)
		}
	}
	return commands
}

// The commands whose absence the gateway reports as "needs an update" are
// shared with the client Fake of an older host, which asserts the same file.
func TestTheCommandsAnOlderHostLacksAreExactlyTheCorpusOnes(t *testing.T) {
	payload, err := os.ReadFile("../../../proto/testdata/older_host/unsupported_commands.json")
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct{ Commands []string }
	if err := json.Unmarshal(payload, &corpus); err != nil {
		t.Fatal(err)
	}
	var all []string
	all = append(all, hostCommands(&apiv1.ReadRequest{}, hostoperations.DispatchHostRead)...)
	all = append(all, hostCommands(&apiv1.MutateRequest{}, hostoperations.DispatchHostMutate)...)
	all = append(all, hostCommands(&apiv1.WatchRequest{}, hostoperations.DispatchHostWatch)...)
	all = append(all, hostCommands(&apiv1.HostCallRequest{}, hostoperations.DispatchHostCall)...)
	if len(all) < 20 {
		t.Fatalf("only %d commands found", len(all))
	}
	for _, cli := range all {
		older := hostCommandAbsentFrom(cli)
		result, err := older.result()
		category, _ := commandFailure(cli, result, err)
		want := hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL
		if slices.Contains(corpus.Commands, cli) {
			want = hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
		}
		if category != want {
			t.Errorf("%s on a checkout that lacks it: %v, want %v", cli, category, want)
		}
	}
	for _, cli := range corpus.Commands {
		if !slices.Contains(all, cli) {
			t.Errorf("the corpus names %q, which no method runs", cli)
		}
	}
}

// hostCommandAbsentFrom is what an older checkout does for cli: argparse rejects it.
func hostCommandAbsentFrom(cli string) failedCommand {
	return failedCommand{
		cli: cli, exitCode: usageExitStatus, rejected: []string{cli},
		stderr: "vibesys: error: unrecognized arguments: host " + cli,
	}
}
