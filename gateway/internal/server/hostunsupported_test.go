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

// argparse is what a checkout that predates a command writes on stderr.
const argparse = "usage: vibesys [-h] ...\nvibesys: error: unrecognized arguments: host list-examples"

// An older checkout: every command exits with argparse's usage status and a
// usage dump, and writes nothing on stdout.
func olderCheckout() hostcommand.FakeRunner {
	return hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		return hostcommand.Result{ExitCode: usageExitStatus, Stderr: argparse}, &exec.ExitError{}
	}}
}

func assertNoUsageDump(t *testing.T, text string) {
	t.Helper()
	for _, leaked := range []string{"usage:", "unrecognized arguments", "argparse"} {
		if strings.Contains(text, leaked) {
			t.Errorf("%q leaks %q", text, leaked)
		}
	}
}

func TestEveryEntryPointTypesAnOlderCheckoutAsUnsupportedWithoutItsUsageDump(t *testing.T) {
	service := &host{config: Config{Root: t.TempDir(), HostRunner: olderCheckout()}}
	unsupported := hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
	ctx := context.Background()

	read, err := service.Read(ctx, &apiv1.ReadRequest{
		Checkout: "/src/checkout",
		Method:   &apiv1.ReadRequest_ListExamples{ListExamples: &hostv1.ListExamplesRequest{RequestId: "list-a"}},
	})
	if err != nil || read.GetFailure().GetCategory() != unsupported {
		t.Fatalf("Read: %v %v", read, err)
	}
	assertNoUsageDump(t, read.GetFailure().GetMessage())

	mutate, err := service.Mutate(ctx, &apiv1.MutateRequest{
		Checkout: "/src/checkout",
		Method:   &apiv1.MutateRequest_RemoveCluster{RemoveCluster: &hostv1.RemoveClusterRequest{RequestId: "rm-a", Name: "cluster-a"}},
	})
	if err != nil || mutate.GetFailure().GetCategory() != unsupported {
		t.Fatalf("Mutate: %v %v", mutate, err)
	}
	assertNoUsageDump(t, mutate.GetFailure().GetMessage())

	call, err := service.HostCall(ctx, &apiv1.HostCallRequest{
		Checkout: "/src/checkout",
		Method: &apiv1.HostCallRequest_PostIntent{PostIntent: &apiv1.PostIntentRequest{
			ExpectedEpoch: 1, ProjectRoot: "/project", RunId: "run-1", RequestId: "r-1",
			Intent: &apiv1.PostIntentRequest_Pause{Pause: &apiv1.PauseIntent{}},
		}},
	})
	failed := call.GetConfigurationFailed()
	if err != nil || failed.GetCode() != "host_unsupported" || failed.GetStderr() != "" {
		t.Fatalf("HostCall: %v %v", call, err)
	}
	assertNoUsageDump(t, failed.GetMessage())
}

func TestWatchTypesAnOlderCheckoutAsUnsupportedWithoutItsUsageDump(t *testing.T) {
	runner := hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		return hostcommand.Result{}, &hostcommand.ExitError{Err: errors.New("exit status 2"), Code: usageExitStatus, Stderr: argparse}
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
	if len(stream.sent) != 1 || stream.sent[0].GetFailure().GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED {
		t.Fatalf("sent %+v", stream.sent)
	}
	assertNoUsageDump(t, stream.sent[0].GetFailure().GetMessage())
}

func TestOtherFailuresKeepTheirDiagnostics(t *testing.T) {
	for name, program := range map[string]hostcommand.Result{
		"crash":                     {ExitCode: 1, Stderr: "Traceback boom"},
		"usage status with a reply": {ExitCode: usageExitStatus, Output: []byte{0x0a}, Stderr: "Traceback boom"},
		"killed by a signal":        {ExitCode: -1, Stderr: "Traceback boom"},
	} {
		t.Run(name, func(t *testing.T) {
			category, message := commandFailure("list-examples", program, &exec.ExitError{})
			if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL || !strings.Contains(message, "Traceback boom") {
				t.Fatalf("%v %q", category, message)
			}
		})
	}
	category, _ := commandFailure("list-examples", hostcommand.Result{ExitCode: usageExitStatus}, errors.New("fork failed"))
	if category != hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL {
		t.Fatalf("a start failure is %v", category)
	}
}
