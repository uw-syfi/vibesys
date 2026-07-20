package httpdriver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"vibesys/microservice-evaluator/api"
)

func TestClientSendsRequestAndPreservesResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Query().Get("q") != "value" {
			t.Errorf("query q = %q", request.URL.Query().Get("q"))
		}
		if err := request.ParseForm(); err != nil {
			t.Error(err)
		}
		if request.Form.Get("name") != "train" {
			t.Errorf("form name = %q", request.Form.Get("name"))
		}
		writer.Header().Set("X-Service-Time", "1.5")
		writer.WriteHeader(http.StatusCreated)
		_, _ = writer.Write([]byte(`{"ok":true}`))
	}))
	defer server.Close()

	opened, err := New().Open(context.Background(), api.Target{
		Address: server.URL, SessionPolicy: "reuse",
	})
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	result := opened.Invoke(context.Background(), api.Invocation{Payload: api.HTTPRequestSpec{
		Method: "POST", Path: "/test", Query: map[string]string{"q": "value"}, Form: map[string]string{"name": "train"},
	}})
	if !result.TransportSuccess || result.NativeStatus != "201" {
		t.Fatalf("unexpected result: %+v", result)
	}
	response := result.Payload.(api.HTTPResponse)
	if response.StatusCode != http.StatusCreated || string(response.Body) != `{"ok":true}` {
		t.Fatalf("unexpected response: %+v", response)
	}
	if values := result.Metadata["X-Service-Time"]; len(values) != 1 || values[0] != "1.5" {
		t.Fatalf("unexpected metadata: %+v", result.Metadata)
	}
}

func TestSessionPolicyControlsConnectionReuse(t *testing.T) {
	var mutex sync.Mutex
	connections := map[string]struct{}{}
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		mutex.Lock()
		connections[request.RemoteAddr] = struct{}{}
		mutex.Unlock()
		_, _ = writer.Write([]byte("ok"))
	}))
	defer server.Close()

	for _, test := range []struct {
		name       string
		policy     string
		wantUnique int
	}{
		{name: "reuse", policy: "reuse", wantUnique: 1},
		{name: "new per request", policy: "new_per_request", wantUnique: 2},
	} {
		t.Run(test.name, func(t *testing.T) {
			mutex.Lock()
			connections = map[string]struct{}{}
			mutex.Unlock()
			opened, err := New().Open(context.Background(), api.Target{Address: server.URL, SessionPolicy: test.policy})
			if err != nil {
				t.Fatal(err)
			}
			for index := 0; index < 2; index++ {
				result := opened.Invoke(context.Background(), api.Invocation{Payload: api.HTTPRequestSpec{Method: "GET", Path: "/"}})
				if !result.TransportSuccess {
					t.Fatalf("request %d failed: %+v", index, result)
				}
			}
			_ = opened.Close()
			mutex.Lock()
			got := len(connections)
			mutex.Unlock()
			if got != test.wantUnique {
				t.Fatalf("unique connections = %d, want %d", got, test.wantUnique)
			}
		})
	}
}

func TestClientClassifiesDeadline(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		time.Sleep(50 * time.Millisecond)
		_, _ = writer.Write([]byte("late"))
	}))
	defer server.Close()
	opened, err := New().Open(context.Background(), api.Target{Address: server.URL, SessionPolicy: "reuse"})
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	result := opened.Invoke(ctx, api.Invocation{Payload: api.HTTPRequestSpec{Method: "GET", Path: "/"}})
	if result.TransportSuccess || result.ErrorCategory != "timeout" {
		t.Fatalf("unexpected result: %+v", result)
	}
}
