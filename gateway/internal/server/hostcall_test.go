package server_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/protobuf/proto"
)

func intentRequest() *apiv1.HostCallRequest {
	return &apiv1.HostCallRequest{Checkout: "/src/host checkout", Method: &apiv1.HostCallRequest_PostIntent{PostIntent: &apiv1.PostIntentRequest{ProjectRoot: "/project", RunId: "run-1", RequestId: "r-1", Intent: &apiv1.PostIntentRequest_Pause{Pause: &apiv1.PauseIntent{}}}}}
}

func TestHostCallPassesTypedPipesAndConfiguredCheckout(t *testing.T) {
	request := intentRequest()
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		if _, ok := ctx.Deadline(); !ok {
			t.Error("missing deadline")
		}
		if c.Executable != filepath.Join(request.Checkout, ".venv", "bin", "vibesys") || strings.Join(c.Args, " ") != "host post-intent" {
			t.Errorf("wrong command: %+v", c)
		}
		if !filepath.IsAbs(c.StateRoot) {
			t.Error("missing run index root")
		}
		decoded := &apiv1.HostCallRequest{}
		if err := proto.Unmarshal(c.Input, decoded); err != nil || !proto.Equal(request, decoded) {
			t.Errorf("stdin: %v %v", decoded, err)
		}
		output, _ := proto.Marshal(&apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_PostIntent{PostIntent: &apiv1.PostIntentResponse{RequestId: "r-1", OrderedReader: true}}})
		return hostcommand.Result{Output: output}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	connection := client(t, f, false)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type reply struct {
		response *apiv1.HostCallResponse
		err      error
	}
	done := make(chan reply, 1)
	go func() {
		response, err := connection.HostCall(ctx, request)
		done <- reply{response, err}
	}()
	// The RPC deliberately has no client deadline: only the gateway can have
	// supplied the runner's deadline. The fixture deadline is a hang guard.
	select {
	case got := <-done:
		if got.err != nil || got.response.GetPostIntent().GetRequestId() != "r-1" || !got.response.GetPostIntent().GetOrderedReader() {
			t.Fatalf("HostCall: %v %v", got.response, got.err)
		}
	case <-f.ctx.Done():
		t.Fatal("HostCall did not return")
	}
}

func TestHostCallFailuresKeepConfigurationShapeAndStderr(t *testing.T) {
	for _, mode := range []string{"missing", "nonzero", "malformed", "empty", "wrong-id", "typed"} {
		t.Run(mode, func(t *testing.T) {
			runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
				result := hostcommand.Result{Stderr: "diagnostic\xff"}
				switch mode {
				case "missing":
					return result, errors.New("executable missing")
				case "nonzero":
					result.ExitCode = 9
				case "malformed":
					result.Output = []byte{0xff}
				case "empty":
				case "wrong-id":
					result.Output, _ = proto.Marshal(&apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_PostIntent{PostIntent: &apiv1.PostIntentResponse{RequestId: "other"}}})
				case "typed":
					result.Output, _ = proto.Marshal(&apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_ConfigurationFailed{ConfigurationFailed: &apiv1.ConfigurationFailed{Code: "inbox_closed", Stage: "post_intent", Message: "run ended", ExitCode: 3}}})
				}
				return result, nil
			}}
			f := startWithConfig(t, server.Config{HostRunner: runner})
			response, err := client(t, f, false).HostCall(f.ctx, intentRequest())
			failure := response.GetConfigurationFailed()
			if err != nil || failure == nil || failure.Code == "" || failure.Stage == "" || failure.Message == "" || failure.Stderr != "diagnostic�" {
				t.Fatalf("failure: %v %v", response, err)
			}
			if mode == "typed" && (failure.Code != "inbox_closed" || failure.ExitCode != 3) {
				t.Fatalf("lost product diagnostic: %v", failure)
			}
		})
	}
}

func TestHostCallRejectsRoutingBeforeSpawning(t *testing.T) {
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		t.Error("invalid routing spawned a process")
		return hostcommand.Result{}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	for _, checkout := range []string{"", "relative", "/path\x00bad", "/path\nbad", strings.Repeat("/", 4097)} {
		request := intentRequest()
		request.Checkout = checkout
		response, err := client(t, f, false).HostCall(f.ctx, request)
		if err != nil || response.GetConfigurationFailed() == nil {
			t.Fatalf("invalid checkout: %v %v", response, err)
		}
	}
	response, err := client(t, f, false).HostCall(f.ctx, &apiv1.HostCallRequest{Checkout: "/src"})
	if err != nil || response.GetConfigurationFailed() == nil {
		t.Fatalf("invalid method: %v %v", response, err)
	}
}

func TestHostCallCancellationReachesRunner(t *testing.T) {
	entered, reaped := make(chan struct{}), make(chan struct{})
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		close(entered)
		<-ctx.Done()
		close(reaped)
		return hostcommand.Result{ExitCode: -1}, ctx.Err()
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	ctx, cancel := context.WithCancel(f.ctx)
	done := make(chan error, 1)
	go func() { _, err := client(t, f, false).HostCall(ctx, intentRequest()); done <- err }()
	select {
	case <-entered:
	case <-time.After(10 * time.Second):
		t.Fatal("runner did not start")
	}
	cancel()
	select {
	case <-reaped:
	case <-time.After(10 * time.Second):
		t.Fatal("runner not cancelled")
	}
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancel succeeded")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("call did not stop")
	}
}

func TestHostCallRelaunchDispatchesWithoutRunPolicy(t *testing.T) {
	for _, mode := range []string{"epoch", "locked", "ended", "checkout", "wrong-method", "missing"} {
		t.Run(mode, func(t *testing.T) {
			request := &apiv1.HostCallRequest{Checkout: "/src/checkout", Method: &apiv1.HostCallRequest_Relaunch{Relaunch: &apiv1.RelaunchRequest{ProjectRoot: "/project", RunId: "run-1", ExpectedEpoch: 1}}}
			runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
				if strings.Join(c.Args, " ") != "host relaunch" || c.Executable != "/src/checkout/.venv/bin/vibesys" {
					t.Fatalf("command: %+v", c)
				}
				decoded := &apiv1.HostCallRequest{}
				if err := proto.Unmarshal(c.Input, decoded); err != nil || !proto.Equal(decoded, request) {
					t.Fatalf("stdin: %v %v", decoded, err)
				}
				if mode == "missing" {
					return hostcommand.Result{ExitCode: -1}, os.ErrNotExist
				}
				result := &apiv1.RelaunchResponse{Outcome: &apiv1.RelaunchResponse_Epoch{Epoch: 2}}
				switch mode {
				case "locked":
					result.Outcome = &apiv1.RelaunchResponse_Refusal{Refusal: apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_LOCKED}
				case "ended":
					result.Outcome = &apiv1.RelaunchResponse_Refusal{Refusal: apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_ENDED}
				case "checkout":
					result.Outcome = &apiv1.RelaunchResponse_Refusal{Refusal: apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_CHECKOUT_MISSING}
				}
				response := &apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_Relaunch{Relaunch: result}}
				if mode == "wrong-method" {
					response.Result = &apiv1.HostCallResponse_PostIntent{PostIntent: &apiv1.PostIntentResponse{RequestId: "r-1"}}
				}
				output, err := proto.Marshal(response)
				return hostcommand.Result{Output: output}, err
			}}
			f := startWithConfig(t, server.Config{HostRunner: runner})
			response, err := client(t, f, false).HostCall(f.ctx, request)
			if err != nil {
				t.Fatal(err)
			}
			if mode == "wrong-method" {
				if response.GetConfigurationFailed().GetCode() != "invalid_host_response" {
					t.Fatal(response)
				}
			} else if response.GetRelaunch() == nil {
				t.Fatal(response)
			}
			if mode == "epoch" && response.GetRelaunch().GetEpoch() != 2 {
				t.Fatal(response)
			}
			if mode == "missing" && response.GetRelaunch().GetRefusal() != apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_CHECKOUT_MISSING {
				t.Fatal(response)
			}
		})
	}
}
