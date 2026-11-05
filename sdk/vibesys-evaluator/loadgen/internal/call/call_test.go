package call_test

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"io"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/build"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
)

// kvServer logs in (POST /login -> {"token": ...}) and serves GET /kv/<key> to a bearer of it.
func kvServer(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("POST /login", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var login struct{ User string }
		if json.Unmarshal(body, &login) != nil || login.User == "" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		w.Header().Set("X-Session", "s-"+login.User)
		_, _ = w.Write([]byte(`{"token":"t-` + login.User + `","n":[1,{"x":true}]}`))
	})
	mux.HandleFunc("GET /kv/{key}", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer t-alice" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(r.PathValue("key") + "?" + r.URL.RawQuery))
	})
	mux.HandleFunc("POST /form", func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		_, _ = w.Write([]byte(r.Header.Get("Content-Type") + " " + r.PostForm.Get("a")))
	})
	mux.HandleFunc("GET /slow", func(w http.ResponseWriter, r *http.Request) {
		<-r.Context().Done()
	})
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server
}

func compile(t *testing.T, keep bool, steps ...*pb.Step) *plan.Op {
	t.Helper()
	op := build.Op(0, steps...)
	op.KeepBodies = keep
	p, err := plan.New([]string{"op"}, []*pb.PlannedOperation{op})
	if err != nil {
		t.Fatal(err)
	}
	return &p.Ops[0]
}

func client(t *testing.T, server *httptest.Server, timeout time.Duration) *call.Client {
	t.Helper()
	c, err := call.NewClient(server.URL, call.Options{Timeout: timeout, Conns: 4})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(c.Close)
	return c
}

func login(user string) *pb.Step {
	return build.CaptureJSON(build.Post(build.Text(`{"user":"`, user, `"}`), "/login"), "token", "/token")
}

func TestTemplateCarriesCapturedTokenToTheNextStep(t *testing.T) {
	server := kvServer(t)
	read := build.Query(build.Header(build.Get("/kv/k1"), "Authorization", "Bearer ", build.Raw("token")), "q", "a b&", build.Raw("token"))
	outcome := client(t, server, 5*time.Second).Do(context.Background(), compile(t, true, login("alice"), read))
	if outcome.Error != pb.ErrorKind_ERROR_KIND_UNSPECIFIED || len(outcome.Steps) != 2 {
		t.Fatalf("outcome %v", outcome)
	}
	if got := string(outcome.Steps[1].GetBody()); got != "k1?q=a+b%26t-alice" {
		t.Fatalf("second step body %q", got)
	}
	if outcome.Start.After(outcome.End) || outcome.Steps[0].GetEndUnixNanos() > outcome.Steps[1].GetStartUnixNanos() {
		t.Fatalf("times out of order: %v", outcome)
	}
}

func TestWrongTokenFailsWithStatusAndUnkeptBodiesAreDigested(t *testing.T) {
	server := kvServer(t)
	read := build.Header(build.Get("/kv/k1"), "Authorization", "Bearer ", build.Raw("token"))
	outcome := client(t, server, 5*time.Second).Do(context.Background(), compile(t, false, login("bob"), read))
	if outcome.Error != pb.ErrorKind_ERROR_KIND_STATUS || outcome.Steps[1].GetStatus() != 401 {
		t.Fatalf("outcome %v", outcome)
	}
	want := sha256.Sum256([]byte(`{"token":"t-bob","n":[1,{"x":true}]}`))
	digest := outcome.Steps[0].GetDigest()
	if digest == nil || string(digest.GetSha256()) != string(want[:]) || digest.GetLength() != 36 {
		t.Fatalf("digest %v", digest)
	}
}

func TestFailedCaptureStopsTheOperation(t *testing.T) {
	server := kvServer(t)
	missing := build.CaptureJSON(build.Post(build.Text(`{"user":"alice"}`), "/login"), "token", "/nothing")
	outcome := client(t, server, 5*time.Second).Do(context.Background(), compile(t, true, missing, build.Get("/kv/", build.Raw("token"))))
	if outcome.Error != pb.ErrorKind_ERROR_KIND_CAPTURE || len(outcome.Steps) != 1 || outcome.Steps[0].GetErrorMessage() == "" {
		t.Fatalf("outcome %v", outcome)
	}
}

func TestHeaderCaptureAndFormBody(t *testing.T) {
	server := kvServer(t)
	first := build.Post(build.Text(`{"user":"alice"}`), "/login")
	first.Captures = append(first.Captures, &pb.Capture{Name: "session", Source: &pb.Capture_Header{Header: "x-session"}})
	form := build.HTTP(pb.HttpMethod_HTTP_METHOD_POST, "/form")
	form.GetHttp().Body = &pb.HttpCall_Form{Form: &pb.Form{Fields: []*pb.Param{{Name: "a", Value: build.Text(build.Raw("session"), "&=")}}}}
	outcome := client(t, server, 5*time.Second).Do(context.Background(), compile(t, true, first, form))
	if got := string(outcome.Steps[1].GetBody()); got != "application/x-www-form-urlencoded s-alice&=" {
		t.Fatalf("form echo %q (%v)", got, outcome)
	}
}

func TestStepTimeoutAndPhaseCancellation(t *testing.T) {
	server := kvServer(t)
	op := compile(t, true, build.Get("/slow"))
	if outcome := client(t, server, 50*time.Millisecond).Do(context.Background(), op); outcome.Error != pb.ErrorKind_ERROR_KIND_TIMEOUT {
		t.Fatalf("timeout: %v", outcome)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if outcome := client(t, server, 5*time.Second).Do(ctx, op); outcome.Error != pb.ErrorKind_ERROR_KIND_CANCELLED {
		t.Fatalf("cancelled: %v", outcome)
	}
}

func TestTransportErrorWhenNothingListens(t *testing.T) {
	server := kvServer(t)
	c := client(t, server, time.Second)
	server.Close()
	if outcome := c.Do(context.Background(), compile(t, true, build.Get("/"))); outcome.Error != pb.ErrorKind_ERROR_KIND_TRANSPORT || outcome.Steps[0].GetStatus() != 0 {
		t.Fatalf("outcome %v", outcome)
	}
}

// Property: Lookup returns, for every path into a generated document, the value encoding/json
// finds there; strings come back unquoted in Text and quoted in JSON.
func TestLookupAgreesWithEncodingJSON(t *testing.T) {
	for seed := range uint64(100) {
		random := rand.New(rand.NewPCG(seed, 4))
		document := randomJSON(random, 3)
		encoded, _ := json.Marshal(document)
		var walk func(value any, path []string)
		walk = func(value any, path []string) {
			if len(path) > 0 {
				got, err := call.Lookup(encoded, path)
				if err != nil {
					t.Fatalf("%s %v: %v", encoded, path, err)
				}
				want, _ := json.Marshal(value)
				if got.JSON != string(want) {
					t.Fatalf("%s %v: JSON %s want %s", encoded, path, got.JSON, want)
				}
				if s, ok := value.(string); ok && got.Text != s {
					t.Fatalf("%s %v: Text %q want %q", encoded, path, got.Text, s)
				}
			}
			switch v := value.(type) {
			case map[string]any:
				for key, child := range v {
					walk(child, append(append([]string(nil), path...), key))
				}
			case []any:
				for i, child := range v {
					walk(child, append(append([]string(nil), path...), itoa(i)))
				}
			}
		}
		walk(document, nil)
		if _, err := call.Lookup(encoded, []string{"no such member \x00"}); err == nil {
			t.Fatalf("%s: lookup of a missing member succeeded", encoded)
		}
	}
}

func itoa(i int) string {
	b, _ := json.Marshal(i)
	return string(b)
}

func randomJSON(random *rand.Rand, depth int) any {
	switch kind := random.IntN(6); {
	case depth > 0 && kind == 0:
		object := map[string]any{}
		for range 1 + random.IntN(3) {
			object[string([]byte{byte('a' + random.IntN(5)), '/', '~'})[:1+random.IntN(3)]] = randomJSON(random, depth-1)
		}
		return object
	case depth > 0 && kind == 1:
		array := []any{}
		for range 1 + random.IntN(3) {
			array = append(array, randomJSON(random, depth-1))
		}
		return array
	case kind == 2:
		return "s\"\\é" + string(rune('a'+random.IntN(26)))
	case kind == 3:
		return random.Float64() * 1000
	case kind == 4:
		return random.IntN(2) == 0
	default:
		return nil
	}
}
