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
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func intentRequest() *apiv1.HostCallRequest {
	return &apiv1.HostCallRequest{Checkout: "/src/host checkout", Method: &apiv1.HostCallRequest_PostIntent{PostIntent: &apiv1.PostIntentRequest{ExpectedEpoch: 1, ProjectRoot: "/project", RunId: "run-1", RequestId: "r-1", Intent: &apiv1.PostIntentRequest_Pause{Pause: &apiv1.PauseIntent{}}}}}
}

func TestHostCallPassesTypedPipesAndConfiguredCheckout(t *testing.T) {
	request := intentRequest()
	request.Deadline = timestamppb.New(time.Unix(1, 0))
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
		if err := proto.Unmarshal(c.Input, decoded); err != nil {
			t.Fatal(err)
		}
		deadline, _ := ctx.Deadline()
		if decoded.Deadline == nil || !decoded.Deadline.AsTime().Equal(deadline) {
			t.Fatalf("helper did not inherit runner deadline: %v", decoded.Deadline)
		}
		decoded.Deadline = request.Deadline
		if !proto.Equal(request, decoded) {
			t.Errorf("stdin: %v", decoded)
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
	for _, mode := range []string{"epoch", "locked", "checkout", "wrong-method", "missing"} {
		t.Run(mode, func(t *testing.T) {
			request := &apiv1.HostCallRequest{Checkout: "/src/checkout", Method: &apiv1.HostCallRequest_Relaunch{Relaunch: &apiv1.RelaunchRequest{ProjectRoot: "/project", RunId: "run-1", ExpectedEpoch: 1}}}
			runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
				if strings.Join(c.Args, " ") != "host relaunch" || c.Executable != "/src/checkout/.venv/bin/vibesys" {
					t.Fatalf("command: %+v", c)
				}
				decoded := &apiv1.HostCallRequest{}
				if err := proto.Unmarshal(c.Input, decoded); err != nil {
					t.Error(err)
				}
				deadline, _ := ctx.Deadline()
				if decoded.Deadline == nil || !decoded.Deadline.AsTime().Equal(deadline) {
					t.Errorf("helper deadline differs from runner: %v", decoded.Deadline)
				}
				decoded.Deadline = nil
				if !proto.Equal(decoded, request) {
					t.Errorf("stdin: %v", decoded)
				}
				if mode == "missing" {
					return hostcommand.Result{ExitCode: -1}, os.ErrNotExist
				}
				result := &apiv1.RelaunchResponse{Outcome: &apiv1.RelaunchResponse_Epoch{Epoch: 2}}
				switch mode {
				case "locked":
					result.Outcome = &apiv1.RelaunchResponse_Refusal{Refusal: apiv1.RelaunchRefusal_RELAUNCH_REFUSAL_LOCKED}
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

func TestHostCallCapsComeFromTheRegistry(t *testing.T) {
	intent, ok := hostoperations.DispatchHostCall(intentRequest())
	if !ok || intent.CLI != "post-intent" || intent.Deadline != 10*time.Second {
		t.Fatalf("post-intent: %+v %v", intent, ok)
	}
	relaunch, ok := hostoperations.DispatchHostCall(&apiv1.HostCallRequest{Method: &apiv1.HostCallRequest_Relaunch{Relaunch: &apiv1.RelaunchRequest{}}})
	if !ok || relaunch.CLI != "relaunch" || relaunch.Deadline != 30*time.Second {
		t.Fatalf("relaunch: %+v %v", relaunch, ok)
	}
	chat, ok := hostoperations.DispatchHostCall(&apiv1.HostCallRequest{Method: &apiv1.HostCallRequest_ChatMessage{ChatMessage: &apiv1.ChatMessageRequest{RequestId: "r-1", SessionId: "s-1"}}})
	if !ok || chat.CLI != "chat-message" || chat.Deadline != 10*time.Second {
		t.Fatalf("chat-message: %+v %v", chat, ok)
	}
	if _, ok := hostoperations.DispatchHostCall(&apiv1.HostCallRequest{}); ok {
		t.Fatal("missing method dispatched")
	}
	response := &apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_ChatMessage{ChatMessage: &apiv1.ChatMessageResponse{RequestId: "r-1", SessionId: "other"}}}
	if hostoperations.HostCallResponseValid(&apiv1.HostCallRequest{Method: &apiv1.HostCallRequest_ChatMessage{ChatMessage: &apiv1.ChatMessageRequest{RequestId: "r-1", SessionId: "s-1"}}}, response) {
		t.Fatal("stale session accepted")
	}
}

func TestHostCallLostAdmissionIsExplicitlyUnconfirmed(t *testing.T) {
	for _, failure := range []error{context.DeadlineExceeded, context.Canceled} {
		t.Run(failure.Error(), func(t *testing.T) {
			runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
				return hostcommand.Result{ExitCode: -1, Stderr: "process reaped"}, failure
			}}
			f := startWithConfig(t, server.Config{HostRunner: runner})
			response, err := client(t, f, false).HostCall(f.ctx, intentRequest())
			if err != nil || response.GetConfigurationFailed().GetCode() != "admission_unconfirmed" {
				t.Fatalf("lost admission reported as a refusal: %v %v", response, err)
			}
		})
	}
}

func readRequest() *apiv1.ReadRequest {
	return &apiv1.ReadRequest{Checkout: "/src/checkout", Method: &apiv1.ReadRequest_ListTasks{ListTasks: &hostv1.ListTasksRequest{RequestId: "request-a", Project: "/project"}}}
}

func TestHostReadUsesTheRegistryCommandAndCap(t *testing.T) {
	request := readRequest()
	dispatched, ok := hostoperations.DispatchHostRead(request)
	if !ok || dispatched.CLI != "list-tasks" || dispatched.Deadline != 10*time.Second {
		t.Fatalf("list-tasks: %+v %v", dispatched, ok)
	}
	machine := &apiv1.ReadRequest{Method: &apiv1.ReadRequest_DescribeMachine{DescribeMachine: &hostv1.DescribeMachineRequest{RequestId: "request-a"}}}
	described, ok := hostoperations.DispatchHostRead(machine)
	if !ok || described.CLI != "describe-machine" || described.Deadline != 25*time.Second {
		t.Fatalf("describe-machine: %+v %v", described, ok)
	}
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		if _, hasDeadline := ctx.Deadline(); !hasDeadline {
			t.Error("missing deadline")
		}
		if c.Executable != filepath.Join(request.Checkout, ".venv", "bin", "vibesys") || strings.Join(c.Args, " ") != "host list-tasks" {
			t.Errorf("wrong command: %+v", c)
		}
		if !filepath.IsAbs(c.StateRoot) {
			t.Error("missing run index root")
		}
		decoded := &apiv1.ReadRequest{}
		if err := proto.Unmarshal(c.Input, decoded); err != nil || !proto.Equal(request, decoded) {
			t.Errorf("stdin: %v %v", decoded, err)
		}
		output, _ := proto.Marshal(&apiv1.ReadResponse{Result: &apiv1.ReadResponse_ListTasks{ListTasks: &hostv1.ListTasksResponse{RequestId: "request-a", Tasks: &hostv1.TaskList{Version: 1}}}})
		return hostcommand.Result{Output: output}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Read(f.ctx, request)
	if err != nil || response.GetListTasks().GetRequestId() != "request-a" {
		t.Fatalf("Read: %v %v", response, err)
	}
}

func TestHostReadDeadlineIsNotAnUnconfirmedAdmission(t *testing.T) {
	for _, failure := range []error{context.DeadlineExceeded, context.Canceled} {
		t.Run(failure.Error(), func(t *testing.T) {
			runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
				return hostcommand.Result{ExitCode: -1}, failure
			}}
			f := startWithConfig(t, server.Config{HostRunner: runner})
			response, err := client(t, f, false).Read(f.ctx, readRequest())
			got := response.GetFailure().GetCategory()
			want := hostv1.FailureCategory_FAILURE_CATEGORY_DEADLINE_EXCEEDED
			if errors.Is(failure, context.Canceled) {
				want = hostv1.FailureCategory_FAILURE_CATEGORY_CANCELLED
			}
			if err != nil || got != want || response.GetFailure().GetMessage() == "" {
				t.Fatalf("read failure: %v %v", response, err)
			}
		})
	}
}

func TestHostReadRejectsAMismatchedIdentity(t *testing.T) {
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		output, _ := proto.Marshal(&apiv1.ReadResponse{Result: &apiv1.ReadResponse_ListTasks{ListTasks: &hostv1.ListTasksResponse{RequestId: "other"}}})
		return hostcommand.Result{Output: output}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Read(f.ctx, readRequest())
	if err != nil || response.GetFailure().GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT {
		t.Fatalf("mismatch: %v %v", response, err)
	}
	spawned := false
	refusing := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		spawned = true
		return hostcommand.Result{}, nil
	}}
	gateway := startWithConfig(t, server.Config{HostRunner: refusing})
	missing, err := client(t, gateway, false).Read(gateway.ctx, &apiv1.ReadRequest{Checkout: "/src/checkout"})
	if err != nil || spawned || missing.GetFailure().GetCategory() != hostv1.FailureCategory_FAILURE_CATEGORY_INVALID_INPUT {
		t.Fatalf("missing method spawned or failed open: %v %v %v", missing, err, spawned)
	}
}

func TestTerminateRunInheritsRunnerDeadline(t *testing.T) {
	request := &apiv1.MutateRequest{Checkout: "/src/checkout", Method: &apiv1.MutateRequest_TerminateRun{TerminateRun: &hostv1.TerminateRunRequest{
		RequestId: "r-1", ProjectRoot: "/project", RunId: "run-1", ExpectedProcess: &storev1.RunProcess{Pid: 42, Epoch: 1, StartedAt: timestamppb.New(time.Unix(1, 0))}, Deadline: timestamppb.New(time.Unix(1, 0)),
	}}}
	runner := hostcommand.FakeRunner{Program: func(ctx context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		decoded := &apiv1.MutateRequest{}
		if err := proto.Unmarshal(c.Input, decoded); err != nil {
			return hostcommand.Result{}, err
		}
		deadline, ok := ctx.Deadline()
		if !ok || decoded.GetTerminateRun().GetDeadline() == nil || !decoded.GetTerminateRun().GetDeadline().AsTime().Equal(deadline) {
			t.Errorf("helper did not inherit runner deadline: %v", decoded)
		}
		decoded.GetTerminateRun().Deadline = request.GetTerminateRun().Deadline
		if !proto.Equal(decoded, request) {
			t.Errorf("stdin: %v", decoded)
		}
		output, err := proto.Marshal(&apiv1.MutateResponse{Result: &apiv1.MutateResponse_TerminateRun{TerminateRun: &hostv1.TerminateRunResponse{RequestId: "r-1", Outcome: hostv1.TerminateRunOutcome_TERMINATE_RUN_OUTCOME_REQUESTED_UNCONFIRMED}}})
		return hostcommand.Result{Output: output}, err
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).Mutate(f.ctx, request)
	if err != nil || response.GetTerminateRun().GetOutcome() != hostv1.TerminateRunOutcome_TERMINATE_RUN_OUTCOME_REQUESTED_UNCONFIRMED {
		t.Fatalf("Mutate: %v %v", response, err)
	}
}
