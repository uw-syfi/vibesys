package server

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

// What a checkout that lacks a command writes on stderr, in the two shapes
// argparse has: one that has no `host` command at all, and one whose `host`
// has no such subcommand.
const (
	noHostCommand = "usage: vibesys [-h] ...\nvibesys: error: unrecognized arguments: host inspect-candidate"
	noSubcommand  = "usage: vibesys host [-h] ...\nvibesys host: error: argument command: invalid choice: 'inspect-candidate' (choose from 'list-examples')"
)

// ordinaryUsageExits are failures that exit 2 and write nothing on stdout, yet
// are not a checkout lacking a command.
var ordinaryUsageExits = map[string]string{
	"missing entrypoint":     "/src/checkout/.venv/bin/python: can't open file '/src/checkout/vibesys.py': [Errno 2] No such file or directory",
	"another command's name": "vibesys host: error: argument command: invalid choice: 'list-examples' (choose from 'inspect-candidate')",
	"an unrelated argument":  "vibesys: error: unrecognized arguments: --verbose",
	"silent":                 "",
}

func TestOnlyExplicitUnknownCommandEvidenceTypesAVersionedCommandAsUnsupported(t *testing.T) {
	for name, stderr := range map[string]string{"no host command": noHostCommand, "no subcommand": noSubcommand} {
		t.Run(name, func(t *testing.T) {
			result := hostcommand.Result{ExitCode: usageExitStatus, Stderr: stderr}
			category, message := commandFailure("inspect-candidate", result, &exec.ExitError{})
			if category != hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED {
				t.Fatalf("category %v", category)
			}
			assertNoUsageDump(t, message)
		})
	}
}

func TestAnOrdinaryUsageStatusFailureKeepsItsCategoryAndDiagnostic(t *testing.T) {
	for name, stderr := range ordinaryUsageExits {
		for _, cli := range []string{"inspect-candidate", "list-examples"} {
			t.Run(name+" "+cli, func(t *testing.T) {
				result := hostcommand.Result{ExitCode: usageExitStatus, Stderr: stderr}
				category, message := commandFailure(cli, result, &exec.ExitError{})
				if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(message, stderr) {
					t.Fatalf("%v %q", category, message)
				}
			})
		}
	}
}

func TestAnUnversionedCommandIsNeverUnsupportedWhateverItsStderrSays(t *testing.T) {
	for _, stderr := range []string{noHostCommand, noSubcommand, strings.ReplaceAll(noSubcommand, "inspect-candidate", "list-examples")} {
		result := hostcommand.Result{ExitCode: usageExitStatus, Stderr: stderr}
		category, message := commandFailure("list-examples", result, &exec.ExitError{})
		if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(message, stderr) {
			t.Fatalf("%v %q", category, message)
		}
	}
}

func TestEntryPointsKeepAnOrdinaryUsageStatusFailureAsInternalWithItsDiagnostic(t *testing.T) {
	stderr := ordinaryUsageExits["missing entrypoint"]
	runner := hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		return hostcommand.Result{ExitCode: usageExitStatus, Stderr: stderr}, &exec.ExitError{}
	}}
	service := &host{config: Config{Root: t.TempDir(), HostRunner: runner}}
	internal := hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL
	ctx := context.Background()

	read, err := service.Read(ctx, &apiv1.ReadRequest{
		Checkout: "/src/checkout",
		Method:   &apiv1.ReadRequest_ListExamples{ListExamples: &hostv1.ListExamplesRequest{RequestId: "list-a"}},
	})
	if err != nil || read.GetFailure().GetCategory() != internal || !strings.Contains(read.GetFailure().GetMessage(), stderr) {
		t.Fatalf("Read: %v %v", read, err)
	}
	mutate, err := service.Mutate(ctx, &apiv1.MutateRequest{
		Checkout: "/src/checkout",
		Method:   &apiv1.MutateRequest_RemoveCluster{RemoveCluster: &hostv1.RemoveClusterRequest{RequestId: "rm-a", Name: "cluster-a"}},
	})
	if err != nil || mutate.GetFailure().GetCategory() != internal || !strings.Contains(mutate.GetFailure().GetMessage(), stderr) {
		t.Fatalf("Mutate: %v %v", mutate, err)
	}
	call, err := service.HostCall(ctx, &apiv1.HostCallRequest{
		Checkout: "/src/checkout",
		Method: &apiv1.HostCallRequest_PostIntent{PostIntent: &apiv1.PostIntentRequest{
			ExpectedEpoch: 1, ProjectRoot: "/project", RunId: "run-1", RequestId: "r-1",
			Intent: &apiv1.PostIntentRequest_Pause{Pause: &apiv1.PauseIntent{}},
		}},
	})
	failed := call.GetConfigurationFailed()
	if err != nil || failed.GetCode() == "host_unsupported" || !strings.Contains(failed.GetStderr(), stderr) {
		t.Fatalf("HostCall: %v %v", call, err)
	}
}

func TestWatchKeepsAnOrdinaryUsageStatusFailureAsInternal(t *testing.T) {
	stderr := ordinaryUsageExits["missing entrypoint"]
	runner := hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		return hostcommand.Result{}, &hostcommand.ExitError{Err: errors.New("exit status 2"), Code: usageExitStatus, Stderr: stderr}
	}}
	service := &host{config: Config{Root: t.TempDir(), HostRunner: runner}}
	stream := &watchStream{ctx: context.Background()}
	request := &apiv1.WatchRequest{
		Checkout: "/checkout",
		Method:   &apiv1.WatchRequest_Login{Login: &hostv1.LoginRequest{RequestId: "request-a", Provider: "claude"}},
	}
	if err := service.Watch(request, stream); err != nil {
		t.Fatal(err)
	}
	failure := stream.sent[0].GetFailure()
	if len(stream.sent) != 1 || failure.GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(failure.GetMessage(), stderr) {
		t.Fatalf("sent %+v", stream.sent)
	}
}

func TestOtherFailuresKeepTheirDiagnostics(t *testing.T) {
	for name, program := range map[string]hostcommand.Result{
		"crash":                     {ExitCode: 1, Stderr: "Traceback boom"},
		"usage status with a reply": {ExitCode: usageExitStatus, Output: []byte{0x0a}, Stderr: noHostCommand},
		"killed by a signal":        {ExitCode: -1, Stderr: "Traceback boom"},
	} {
		t.Run(name, func(t *testing.T) {
			category, message := commandFailure("inspect-candidate", program, &exec.ExitError{})
			if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(message, program.Stderr) {
				t.Fatalf("%v %q", category, message)
			}
		})
	}
	category, _ := commandFailure("inspect-candidate", hostcommand.Result{ExitCode: usageExitStatus, Stderr: noHostCommand}, errors.New("fork failed"))
	if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL {
		t.Fatalf("a start failure is %v", category)
	}
}

func assertNoUsageDump(t *testing.T, text string) {
	t.Helper()
	for _, leaked := range []string{"usage:", "unrecognized arguments", "argparse", "invalid choice"} {
		if strings.Contains(text, leaked) {
			t.Errorf("%q leaks %q", text, leaked)
		}
	}
}
