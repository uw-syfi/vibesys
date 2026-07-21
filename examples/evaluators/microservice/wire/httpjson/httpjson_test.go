package httpjson

import (
	"reflect"
	"testing"
)

type testBody struct {
	Zed   string  `json:"zed"`
	Alpha float64 `json:"alpha"`
}

func TestRequestMakesStructAndMapBodiesObservationallyIdentical(t *testing.T) {
	fromStruct, err := Request("POST", "/objects", testBody{Zed: "value", Alpha: 1.25}, "Bearer token")
	if err != nil {
		t.Fatal(err)
	}
	fromMap, err := Request("POST", "/objects", map[string]any{"alpha": 1.25, "zed": "value"}, "Bearer token")
	if err != nil {
		t.Fatal(err)
	}
	if fromStruct.Body != fromMap.Body {
		t.Fatalf("body differs by input representation: %q != %q", fromStruct.Body, fromMap.Body)
	}
	if !reflect.DeepEqual(fromStruct.Headers, fromMap.Headers) {
		t.Fatalf("headers differ by input representation: %v != %v", fromStruct.Headers, fromMap.Headers)
	}
	if fromStruct.Body != `{"alpha":1.25,"zed":"value"}` {
		t.Fatalf("body=%q", fromStruct.Body)
	}
	wantHeaders := map[string]string{
		"Accept": "application/json", "Authorization": "Bearer token", "Content-Type": "application/json",
	}
	if !reflect.DeepEqual(fromStruct.Headers, wantHeaders) {
		t.Fatalf("headers=%v, want %v", fromStruct.Headers, wantHeaders)
	}
}

func TestRequestOmitsBodyHeadersAndAuthorizationWhenAbsent(t *testing.T) {
	spec, err := Request("GET", "/objects", nil, "")
	if err != nil {
		t.Fatal(err)
	}
	if spec.Body != "" || !reflect.DeepEqual(spec.Headers, map[string]string{"Accept": "application/json"}) {
		t.Fatalf("spec=%+v", spec)
	}
}
