package socialnetwork

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"strconv"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
)

type recordingRuntime struct {
	requests []api.HTTPRequestSpec
	result   func(api.HTTPRequestSpec) api.ProtocolResult
}

func (r *recordingRuntime) Invoke(_ context.Context, invocation api.Invocation) api.ProtocolResult {
	request := invocation.Payload.(api.HTTPRequestSpec)
	r.requests = append(r.requests, request)
	if r.result != nil {
		return r.result(request)
	}
	body := []byte("Success")
	if request.Path == "/wrk2-api/post/compose" {
		body = []byte(composeSuccessBody)
	}
	return protocolResult(http.StatusOK, body)
}

func TestNewValidatesApplicationContract(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*api.Workload)
		want   string
	}{
		{"missing gateway", func(w *api.Workload) { w.Targets = nil }, "target named gateway"},
		{"wrong protocol", func(w *api.Workload) { w.Targets[0].Protocol = "grpc" }, "must use HTTP"},
		{"repetitions", func(w *api.Workload) { w.Load.Repetitions = 2 }, "repetitions must be 1"},
		{"unknown config", func(w *api.Workload) { w.ApplicationConfig["userz"] = int64(2) }, "unknown application_config"},
		{"too few users", func(w *api.Workload) { w.ApplicationConfig["users"] = int64(1) }, "at least 2"},
		{"no seed posts", func(w *api.Workload) { w.ApplicationConfig["seed_posts_per_user"] = int64(0) }, "must be positive"},
		{"bad timeline limit", func(w *api.Workload) { w.ApplicationConfig["timeline_limit"] = 1.5 }, "must be an integer"},
		{"invalid setup delay", func(w *api.Workload) { w.ApplicationConfig["setup_delay_seconds"] = math.NaN() }, "non-negative number"},
		{"oversized user ID", func(w *api.Workload) { w.ApplicationConfig["user_id_base"] = int64(math.MaxInt) }, "too large"},
		{"wrong operation target", func(w *api.Workload) { w.Operations[0].Target = "other" }, "must target gateway"},
		{"declarative operation", func(w *api.Workload) { w.Operations[0].HTTP = &api.HTTPRequestSpec{} }, "must not declare"},
		{"unknown operation", func(w *api.Workload) { w.Operations[0].Name = "delete_everything" }, "unknown Social Network operation"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			workload := socialNetworkWorkload(userTimelineRead)
			test.mutate(&workload)
			_, err := New(workload)
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("New() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestPrepareBuildsSeedNamespacedRingFixture(t *testing.T) {
	application := &Application{config: Config{
		Users: 3, SeedPostsPerUser: 2, UserIDBase: 100, UsernamePrefix: "test_", TimelineLimit: 10,
	}}
	runtime := &recordingRuntime{}
	prepared, err := application.Prepare(context.Background(), runtime, api.TrialContext{Index: 1, Seed: 42})
	if err != nil {
		t.Fatal(err)
	}
	data := prepared.(*dataset)
	// Three registrations + three ring edges + six seed posts.
	if len(runtime.requests) != 12 {
		t.Fatalf("setup requests = %d, want 12", len(runtime.requests))
	}
	if data.users[0].followee != 2 || data.users[1].followee != 0 || data.users[2].followee != 1 {
		t.Fatalf("unexpected followee ring: %+v", data.users)
	}
	if got := runtime.requests[0].Form["username"]; got != data.users[0].username || !strings.HasPrefix(got, "test_") {
		t.Fatalf("first registered username = %q, dataset username = %q", got, data.users[0].username)
	}
	if got := runtime.requests[3].Form["followee_name"]; got != data.users[2].username {
		t.Fatalf("user zero followee = %q, want %q", got, data.users[2].username)
	}
	if got := runtime.requests[len(runtime.requests)-1].Form["text"]; got != "seed_"+data.namespace+"_2_1" {
		t.Fatalf("last seed text = %q", got)
	}

	same := application.makeDataset(api.TrialContext{Index: 1, Seed: 42})
	different := application.makeDataset(api.TrialContext{Index: 1, Seed: 43})
	if same.namespace != data.namespace || same.users[0].id != data.users[0].id || same.users[0].username != data.users[0].username {
		t.Fatal("same trial context produced a different fixture namespace")
	}
	if different.namespace == data.namespace || different.users[0].id == data.users[0].id {
		t.Fatal("different trial seed reused fixture identity")
	}
}

func TestPrepareRejectsUnexpectedComposeSuccessAndAllowsOnlySeedZADD(t *testing.T) {
	application := &Application{config: Config{
		Users: 2, SeedPostsPerUser: 1, UserIDBase: 100, UsernamePrefix: "test_", TimelineLimit: 10,
	}}
	runtime := &recordingRuntime{}
	runtime.result = func(api.HTTPRequestSpec) api.ProtocolResult {
		return protocolResult(http.StatusOK, []byte("not successful"))
	}
	if _, err := application.Prepare(context.Background(), runtime, api.TrialContext{}); err == nil || !strings.Contains(err.Error(), "unsuccessful setup response") {
		t.Fatalf("unexpected registration body was accepted: %v", err)
	}
	runtime.result = func(request api.HTTPRequestSpec) api.ProtocolResult {
		if request.Path == "/wrk2-api/post/compose" {
			return protocolResult(http.StatusOK, []byte("not actually composed"))
		}
		return protocolResult(http.StatusOK, []byte("Success"))
	}
	if _, err := application.Prepare(context.Background(), runtime, api.TrialContext{}); err == nil || !strings.Contains(err.Error(), "unexpected compose response") {
		t.Fatalf("unexpected compose body was accepted: %v", err)
	}

	zadd := protocolResult(http.StatusInternalServerError, []byte("first fan-out ZADD failed"))
	runtime.result = func(request api.HTTPRequestSpec) api.ProtocolResult {
		if request.Path == "/wrk2-api/post/compose" {
			return zadd
		}
		return protocolResult(http.StatusOK, []byte("Success"))
	}
	if _, err := application.Prepare(context.Background(), runtime, api.TrialContext{}); err != nil {
		t.Fatalf("known seed-only ZADD failure was rejected: %v", err)
	}
	if validation := validateCompose(zadd); validation.Success {
		t.Fatal("measured compose accepted seed-only ZADD exception")
	}
}

func TestBuildOperationSupportsSkipPrepareWithWorkloadSeed(t *testing.T) {
	workload := socialNetworkWorkload(userTimelineRead)
	workload.Load.Seed = 77
	applicationValue, err := New(workload)
	if err != nil {
		t.Fatal(err)
	}
	application := applicationValue.(*Application)
	plan, err := application.BuildOperation(workload.Operations[0], api.Sample{Random: 1}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer application.FinishOperation(plan)
	want := &application.makeDataset(api.TrialContext{Seed: 77}).users[1]
	request := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
	if request.Query["user_id"] != strconv.Itoa(want.id) || plan.State.(*operationState).user.username != want.username {
		t.Fatalf("skip-prepare plan did not reconstruct fixture for seed 77: %+v", plan)
	}
}

func TestBuildOperationCreatesStatefulPlans(t *testing.T) {
	application, data := testApplicationAndDataset()
	tests := []struct {
		name        string
		invocations int
		firstPath   string
		lastPath    string
	}{
		{userTimelineRead, 1, "/wrk2-api/user-timeline/read", "/wrk2-api/user-timeline/read"},
		{homeTimelineRead, 1, "/wrk2-api/home-timeline/read", "/wrk2-api/home-timeline/read"},
		{composeUserTimeline, 2, "/wrk2-api/post/compose", "/wrk2-api/user-timeline/read"},
		{legacyComposePost, 1, "/wrk2-api/post/compose", "/wrk2-api/post/compose"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			operation := api.Operation{Name: test.name, Target: "gateway"}
			plan, err := application.BuildOperation(operation, api.Sample{Counter: 3, Random: 1}, data)
			if err != nil {
				t.Fatal(err)
			}
			defer application.FinishOperation(plan)
			if len(plan.Invocations) != test.invocations {
				t.Fatalf("invocations = %d, want %d", len(plan.Invocations), test.invocations)
			}
			first := plan.Invocations[0].Payload.(api.HTTPRequestSpec)
			last := plan.Invocations[len(plan.Invocations)-1].Payload.(api.HTTPRequestSpec)
			if first.Path != test.firstPath || last.Path != test.lastPath {
				t.Fatalf("paths = %q ... %q", first.Path, last.Path)
			}
			state := plan.State.(*operationState)
			if test.name == homeTimelineRead && state.user != &data.users[0] {
				t.Fatalf("home timeline expected creator is not selected user's followee")
			}
			if test.name == composeUserTimeline && (state.marker == "" || last.Query["user_id"] != "101") {
				t.Fatalf("stateful compose plan = %+v, read query = %+v", state, last.Query)
			}
		})
	}
}

func TestComposePlanSerializesWithReadsForSameUser(t *testing.T) {
	application, data := testApplicationAndDataset()
	compose, err := application.BuildOperation(
		api.Operation{Name: composeUserTimeline, Target: "gateway"}, api.Sample{Random: 0}, data,
	)
	if err != nil {
		t.Fatal(err)
	}

	readReady := make(chan api.OperationPlan, 1)
	readError := make(chan error, 1)
	go func() {
		plan, buildErr := application.BuildOperation(
			api.Operation{Name: userTimelineRead, Target: "gateway"}, api.Sample{Random: 0}, data,
		)
		if buildErr != nil {
			readError <- buildErr
			return
		}
		readReady <- plan
	}()
	select {
	case <-readReady:
		t.Fatal("read plan acquired fixture while compose plan held its write lock")
	case err := <-readError:
		t.Fatal(err)
	case <-time.After(25 * time.Millisecond):
	}
	application.FinishOperation(compose)
	select {
	case plan := <-readReady:
		application.FinishOperation(plan)
	case err := <-readError:
		t.Fatal(err)
	case <-time.After(time.Second):
		t.Fatal("read plan did not acquire fixture after compose finished")
	}
}

func TestValidateTimelineRejectsSemanticCorruption(t *testing.T) {
	user := &benchmarkUser{id: 101, username: "test_user"}
	valid := func() []map[string]any {
		return []map[string]any{
			validPost(user, "post-2", "marker", "200"),
			validPost(user, "post-1", "older", "100"),
		}
	}
	tests := []struct {
		name     string
		body     func() []byte
		category string
	}{
		{"not array", func() []byte { return []byte(`{"post_id":"x"}`) }, "response_json"},
		{"empty", func() []byte { return []byte(`[]`) }, "response_value"},
		{"missing field", func() []byte { posts := valid(); delete(posts[0], "req_id"); return mustJSON(posts) }, "response_schema"},
		{"wrong creator", func() []byte {
			posts := valid()
			posts[0]["creator"].(map[string]any)["username"] = "other"
			return mustJSON(posts)
		}, "response_value"},
		{"bad timestamp", func() []byte { posts := valid(); posts[0]["timestamp"] = "yesterday"; return mustJSON(posts) }, "response_schema"},
		{"ascending order", func() []byte { posts := valid(); posts[1]["timestamp"] = "300"; return mustJSON(posts) }, "response_order"},
		{"duplicate post", func() []byte { posts := valid(); posts[1]["post_id"] = "post-2"; return mustJSON(posts) }, "response_value"},
		{"bad collection", func() []byte { posts := valid(); posts[0]["media"] = "none"; return mustJSON(posts) }, "response_schema"},
		{"bad collection item", func() []byte {
			posts := valid()
			posts[0]["user_mentions"] = []any{map[string]any{"username": "missing-id"}}
			return mustJSON(posts)
		}, "response_schema"},
		{"marker absent", func() []byte { posts := valid(); posts[0]["text"] = "different"; return mustJSON(posts) }, "response_value"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			validation := validateTimeline(protocolResult(http.StatusOK, test.body()), user, "marker", 10)
			if validation.Success || validation.ErrorCategory != test.category {
				t.Fatalf("validation = %+v, want failure category %q", validation, test.category)
			}
		})
	}
	if validation := validateTimeline(protocolResult(http.StatusOK, mustJSON(valid())), user, "marker", 1); validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("oversized timeline validation = %+v", validation)
	}
	if validation := validateTimeline(protocolResult(http.StatusOK, mustJSON(valid())), user, "marker", 10); !validation.Success {
		t.Fatalf("valid timeline rejected: %+v", validation)
	}
}

func TestValidateComposeReadAndTimingHeaders(t *testing.T) {
	user := &benchmarkUser{id: 101, username: "test_user"}
	operation := api.Operation{
		Name: composeUserTimeline, Target: "gateway",
		CaptureHeaders: []api.HeaderCapture{
			{Name: "compose", Header: "X-Compose-Thrift-Ms", Unit: "ms"},
			{Name: "timeline", Header: "X-UserTimeline-Thrift-Ms", Unit: "ms"},
		},
	}
	plan := api.OperationPlan{State: &operationState{
		kind: composeUserTimeline, user: user, marker: "marker", timelineSize: 10,
	}}
	compose := protocolResult(http.StatusOK, []byte(composeSuccessBody))
	compose.Metadata = map[string][]string{http.CanonicalHeaderKey("X-Compose-Thrift-Ms"): {"1.25"}}
	read := protocolResult(http.StatusOK, mustJSON([]map[string]any{validPost(user, "post", "marker", "100")}))
	read.Metadata = map[string][]string{http.CanonicalHeaderKey("X-UserTimeline-Thrift-Ms"): {"2.5"}}
	validation := (&Application{}).ValidateOperation(operation, plan, []api.ProtocolResult{compose, read})
	if !validation.Success || validation.CustomTimings["compose"] != 1250*time.Microsecond || validation.CustomTimings["timeline"] != 2500*time.Microsecond {
		t.Fatalf("valid compose/read rejected or timings lost: %+v", validation)
	}

	badCompose := compose
	badCompose.Payload = api.HTTPResponse{StatusCode: http.StatusOK, Body: []byte("Success")}
	if validation := (&Application{}).ValidateOperation(operation, plan, []api.ProtocolResult{badCompose, read}); validation.Success || validation.ErrorCategory != "response_value" {
		t.Fatalf("bad compose response accepted: %+v", validation)
	}
	missingMarker := read
	missingMarker.Payload = api.HTTPResponse{StatusCode: http.StatusOK, Body: mustJSON([]map[string]any{validPost(user, "post", "other", "100")})}
	if validation := (&Application{}).ValidateOperation(operation, plan, []api.ProtocolResult{compose, missingMarker}); validation.Success || validation.ErrorCategory != "read_your_write" {
		t.Fatalf("missing read-your-write marker accepted: %+v", validation)
	}
	badTiming := read
	badTiming.Metadata = map[string][]string{http.CanonicalHeaderKey("X-UserTimeline-Thrift-Ms"): {"NaN"}}
	if validation := (&Application{}).ValidateOperation(operation, plan, []api.ProtocolResult{compose, badTiming}); validation.Success || validation.ErrorCategory != "timing_header" {
		t.Fatalf("invalid timing header accepted: %+v", validation)
	}
}

func TestValidateOperationPropagatesTransportAndResultCountFailures(t *testing.T) {
	user := &benchmarkUser{id: 101, username: "test_user"}
	application := &Application{}
	operation := api.Operation{Name: userTimelineRead, Target: "gateway"}
	plan := api.OperationPlan{State: &operationState{kind: userTimelineRead, user: user, timelineSize: 10}}
	mismatched := api.OperationPlan{State: &operationState{kind: homeTimelineRead, user: user, timelineSize: 10}}
	if validation := application.ValidateOperation(operation, mismatched, nil); validation.Success || validation.ErrorCategory != "invalid_plan" {
		t.Fatalf("mismatched plan accepted: %+v", validation)
	}
	if validation := application.ValidateOperation(operation, plan, nil); validation.Success || validation.ErrorCategory != "result_count" {
		t.Fatalf("missing result accepted: %+v", validation)
	}
	result := api.ProtocolResult{ErrorCategory: "timeout", ErrorMessage: "deadline"}
	if validation := application.ValidateOperation(operation, plan, []api.ProtocolResult{result}); validation.Success || validation.ErrorCategory != "timeout" {
		t.Fatalf("transport error lost: %+v", validation)
	}
}

func testApplicationAndDataset() (*Application, *dataset) {
	application := &Application{config: Config{
		Users: 2, SeedPostsPerUser: 1, UserIDBase: 100, UsernamePrefix: "test_", TimelineLimit: 10,
	}}
	return application, &dataset{namespace: "namespace", users: []benchmarkUser{
		{id: 100, username: "test_0", followee: 1},
		{id: 101, username: "test_1", followee: 0},
	}}
}

func socialNetworkWorkload(operationName string) api.Workload {
	return api.Workload{
		Version: 1, Name: "social-network-test", Application: "social-network",
		Load:        api.Load{Model: "open_loop", Rate: 10, DurationSeconds: 1, Concurrency: 2, TimeoutSeconds: 1, Repetitions: 1, MinOfferedRateRatio: 0.5},
		Targets:     []api.Target{{Name: "gateway", Protocol: "http", Address: "http://example.invalid"}},
		Operations:  []api.Operation{{Name: operationName, Target: "gateway", Weight: 1}},
		Objective:   api.Objective{Name: "p50_ms", Metric: "latency_ms.p50", Direction: "minimize", Unit: "ms"},
		Constraints: api.Constraints{},
		ApplicationConfig: map[string]any{
			"users": int64(2), "seed_posts_per_user": int64(1), "setup_delay_seconds": float64(0),
		},
	}
}

func validPost(user *benchmarkUser, postID, text, timestamp string) map[string]any {
	return map[string]any{
		"post_id": postID, "creator": map[string]any{"user_id": strconv.Itoa(user.id), "username": user.username},
		"req_id": "request-" + postID, "text": text, "timestamp": timestamp, "post_type": float64(0),
		"user_mentions": []any{}, "media": map[string]any{}, "urls": []any{},
	}
}

func protocolResult(status int, body []byte) api.ProtocolResult {
	return api.ProtocolResult{
		TransportSuccess: true, NativeStatus: http.StatusText(status),
		Payload: api.HTTPResponse{StatusCode: status, Body: body},
	}
}

func mustJSON(value any) []byte {
	encoded, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return encoded
}
