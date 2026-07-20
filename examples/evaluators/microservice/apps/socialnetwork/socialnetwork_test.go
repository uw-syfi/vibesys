package socialnetwork

import (
	"context"
	"net/http"
	"testing"

	"vibesys/microservice-evaluator/api"
)

type recordingRuntime struct {
	requests []api.HTTPRequestSpec
}

func (r *recordingRuntime) Invoke(_ context.Context, invocation api.Invocation) api.ProtocolResult {
	r.requests = append(r.requests, invocation.Payload.(api.HTTPRequestSpec))
	return api.ProtocolResult{
		TransportSuccess: true,
		NativeStatus:     "200",
		Payload:          api.HTTPResponse{StatusCode: http.StatusOK, Body: []byte(`{"ok":true}`)},
	}
}

func TestPrepareBuildsDeterministicFixture(t *testing.T) {
	application := &Application{config: Config{
		Users: 2, SeedPostsPerUser: 1, UserIDBase: 100, UsernamePrefix: "test_",
	}}
	runtime := &recordingRuntime{}
	if _, err := application.Prepare(context.Background(), runtime, api.TrialContext{}); err != nil {
		t.Fatal(err)
	}
	// 2 registrations + 2 follow edges + 2 seed posts.
	if len(runtime.requests) != 6 {
		t.Fatalf("setup requests = %d, want 6", len(runtime.requests))
	}
	if got := runtime.requests[0].Form["username"]; got != "test_0" {
		t.Fatalf("first username = %q", got)
	}
	if got := runtime.requests[len(runtime.requests)-1].Form["text"]; got != "seed_0" {
		t.Fatalf("last seed text = %q", got)
	}
}

func TestBuildOperationUsesOperationCatalog(t *testing.T) {
	application := &Application{config: Config{Users: 2, UserIDBase: 100, UsernamePrefix: "test_"}}
	plan, err := application.BuildOperation(
		api.Operation{Name: userTimelineRead, Target: "gateway"},
		api.Sample{Random: 1},
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	request := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
	if got := request.Query["user_id"]; got != "101" {
		t.Fatalf("user_id = %q, want 101", got)
	}
}
