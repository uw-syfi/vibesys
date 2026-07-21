package socialnetwork

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"testing"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/drivers/httpdriver"
	"vibesys/microservice-evaluator/engine"
	"vibesys/microservice-evaluator/registry"
)

func TestServiceBenchRunsSocialNetworkOperationsEndToEnd(t *testing.T) {
	target := newFakeSocialNetwork(t)
	server := httptest.NewServer(target)
	defer server.Close()

	for index, operationName := range []string{userTimelineRead, homeTimelineRead, composeUserTimeline} {
		t.Run(operationName, func(t *testing.T) {
			workload := socialNetworkWorkload(operationName)
			workload.Load.Rate = 25
			workload.Load.DurationSeconds = 0.04
			workload.Load.Seed = int64(100 + index)
			workload.Targets[0].Address = server.URL
			workload.Targets[0].SessionPolicy = "reuse"
			workload.Constraints.MinSuccessRate = floatPointer(1)
			if operationName == userTimelineRead {
				workload.Operations[0].CaptureHeaders = []api.HeaderCapture{{
					Name: "user_timeline_thrift", Header: "X-UserTimeline-Thrift-Ms", Unit: "ms",
				}}
			}
			if operationName == homeTimelineRead {
				workload.Operations[0].CaptureHeaders = []api.HeaderCapture{{
					Name: "home_timeline_thrift", Header: "X-HomeTimeline-Thrift-Ms", Unit: "ms",
				}}
			}
			if operationName == composeUserTimeline {
				workload.Operations[0].CaptureHeaders = []api.HeaderCapture{
					{Name: "compose_thrift", Header: "X-Compose-Thrift-Ms", Unit: "ms"},
					{Name: "user_timeline_thrift", Header: "X-UserTimeline-Thrift-Ms", Unit: "ms"},
				}
			}

			components := registry.New()
			if err := components.RegisterDriver(httpdriver.New()); err != nil {
				t.Fatal(err)
			}
			if err := components.RegisterApplication("social-network", New); err != nil {
				t.Fatal(err)
			}
			runner := engine.New(components, engine.Options{EngineVersion: "test", WorkloadHash: "test"})
			result, err := runner.Run(context.Background(), workload)
			if err != nil {
				t.Fatal(err)
			}
			if !result.Summary.Valid || result.Summary.PrimaryValue == nil {
				t.Fatalf("end-to-end run was invalid: %+v", result.Summary)
			}
			if len(result.Observations) != 1 || !result.Observations[0].ApplicationSuccess {
				t.Fatalf("observations = %+v", result.Observations)
			}
			wantInvocations := 1
			if operationName == composeUserTimeline {
				wantInvocations = 2
			}
			observation := result.Observations[0]
			if observation.InvocationCount != wantInvocations || len(observation.CustomTimings) == 0 {
				t.Fatalf("observation did not account for the full operation: %+v", observation)
			}
		})
	}
}

type fakeSocialNetwork struct {
	t        *testing.T
	mu       sync.Mutex
	clock    int64
	users    map[string]int
	names    map[int]string
	followee map[string]string
	posts    map[int][]map[string]any
}

func newFakeSocialNetwork(t *testing.T) *fakeSocialNetwork {
	return &fakeSocialNetwork{
		t: t, clock: 1_000,
		users: make(map[string]int), names: make(map[int]string),
		followee: make(map[string]string), posts: make(map[int][]map[string]any),
	}
}

func (s *fakeSocialNetwork) ServeHTTP(writer http.ResponseWriter, request *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch request.URL.Path {
	case "/wrk2-api/user/register":
		s.register(writer, request)
	case "/wrk2-api/user/follow":
		s.follow(writer, request)
	case "/wrk2-api/post/compose":
		s.compose(writer, request)
	case "/wrk2-api/user-timeline/read":
		s.timeline(writer, request, false)
	case "/wrk2-api/home-timeline/read":
		s.timeline(writer, request, true)
	default:
		http.NotFound(writer, request)
	}
}

func (s *fakeSocialNetwork) register(writer http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		s.t.Errorf("parse register form: %v", err)
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	id, err := strconv.Atoi(request.Form.Get("user_id"))
	if err != nil || request.Form.Get("username") == "" {
		http.Error(writer, "invalid user", http.StatusBadRequest)
		return
	}
	name := request.Form.Get("username")
	s.users[name] = id
	s.names[id] = name
	_, _ = writer.Write([]byte("Success"))
}

func (s *fakeSocialNetwork) follow(writer http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	user := request.Form.Get("user_name")
	followee := request.Form.Get("followee_name")
	if _, ok := s.users[user]; !ok || s.users[followee] == 0 {
		http.Error(writer, "unknown user", http.StatusBadRequest)
		return
	}
	s.followee[user] = followee
	_, _ = writer.Write([]byte("Success"))
}

func (s *fakeSocialNetwork) compose(writer http.ResponseWriter, request *http.Request) {
	if err := request.ParseForm(); err != nil {
		http.Error(writer, err.Error(), http.StatusBadRequest)
		return
	}
	username := request.Form.Get("username")
	id, err := strconv.Atoi(request.Form.Get("user_id"))
	if err != nil || s.users[username] != id {
		http.Error(writer, "unknown user", http.StatusBadRequest)
		return
	}
	s.clock++
	post := validPost(&benchmarkUser{id: id, username: username}, fmt.Sprintf("post-%d", s.clock), request.Form.Get("text"), strconv.FormatInt(s.clock, 10))
	s.posts[id] = append([]map[string]any{post}, s.posts[id]...)
	writer.Header().Set("X-Compose-Thrift-Ms", "1.5")
	_, _ = writer.Write([]byte(composeSuccessBody))
}

func (s *fakeSocialNetwork) timeline(writer http.ResponseWriter, request *http.Request, home bool) {
	id, err := strconv.Atoi(request.URL.Query().Get("user_id"))
	if err != nil || s.names[id] == "" {
		http.Error(writer, "unknown user", http.StatusBadRequest)
		return
	}
	if home {
		id = s.users[s.followee[s.names[id]]]
		writer.Header().Set("X-HomeTimeline-Thrift-Ms", "2")
	} else {
		writer.Header().Set("X-UserTimeline-Thrift-Ms", "1")
	}
	limit, _ := strconv.Atoi(request.URL.Query().Get("stop"))
	posts := s.posts[id]
	if len(posts) > limit {
		posts = posts[:limit]
	}
	writer.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(writer).Encode(posts); err != nil {
		s.t.Errorf("encode timeline: %v", err)
	}
}

func floatPointer(value float64) *float64 { return &value }
