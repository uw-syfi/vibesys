package workload

import (
	"math"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
)

// The JSON texts Python's json.dumps writes for these values (separators "," and ":"), taken
// from CPython 3.12: a JSON body built here is the bytes the Python SDK sends.
func TestJSONIsWhatPythonWrites(t *testing.T) {
	for _, c := range []struct {
		value any
		want  string
	}{
		{"a\"b\\c\n\t\x01\x7f", `"a\"b\\c\n\t\u0001\u007f"`},
		{"\u00e9", `"\u00e9"`},
		{"\U0001F600", `"\ud83d\ude00"`},
		{"<>&/", `"<>&/"`},
		{1.0, "1.0"}, {0.1, "0.1"}, {1e16, "1e+16"}, {1e15, "1000000000000000.0"}, {1e-5, "1e-05"},
		{0.0001, "0.0001"}, {math.Copysign(0, -1), "-0.0"}, {123456789.123, "123456789.123"},
		{1.5e300, "1.5e+300"}, {5e-324, "5e-324"}, {2.5, "2.5"},
		{Object{{"k", []any{1, 2.5, true, nil, "x"}}}, `{"k":[1,2.5,true,null,"x"]}`},
		{map[string]int{"b": 1, "a": 2}, `{"a":2,"b":1}`},
		{struct {
			User     string `json:"user"`
			Password string `json:"password"`
			Skip     int    `json:"-"`
			Empty    string `json:"empty,omitempty"`
			N        uint8
		}{"u", "p", 1, "", 7}, `{"user":"u","password":"p","N":7}`},
	} {
		var parts []part
		jsonParts(c.value, &parts, "json")
		if got := merged(parts).String(); got != c.want {
			t.Errorf("%#v: got %s, want %s", c.value, got, c.want)
		}
	}
}

func stepJSON(t *testing.T, s Step) string {
	t.Helper()
	text, err := protojson.Marshal(s.message)
	if err != nil {
		t.Fatal(err)
	}
	return string(text)
}

func TestStepsAreThePythonSteps(t *testing.T) {
	// Python: post("/login", json={"user": "u0", "password": "p"}, capture={"token": "/token"})
	login := Post("/login", JSON(Object{{"user", "u0"}, {"password", "p"}}), Capture("token", "/token"))
	call := login.message.GetHttp()
	if string(call.GetRaw()) != `{"user":"u0","password":"p"}` || call.GetHeaders()[0].GetName() != "Content-Type" ||
		login.message.GetCaptures()[0].GetJsonPointer() != "/token" || len(login.message.GetExpectStatus()) != 0 {
		t.Fatalf("login %s", stepJSON(t, login))
	}
	// Python: get("/kv/k1", headers={"Authorization": "Bearer " + ref("token")}, expect=(200, 404))
	read := Get("/kv/k1", Header("Authorization", Concat("Bearer ", Ref("token"))), Expect(200, 404))
	parts := read.message.GetHttp().GetHeaders()[0].GetValue().GetParts()
	if len(parts) != 2 || parts[0].GetLiteral() != "Bearer " || parts[1].GetRef().GetEncoding() != pb.RefEncoding_REF_ENCODING_RAW ||
		len(read.message.GetExpectStatus()) != 2 || !read.refs["token"] {
		t.Fatalf("read %s", stepJSON(t, read))
	}
	// A Template inside a JSON body is a JSON string with the value inserted.
	use := Post("/use", JSON(Object{{"token", Ref("token")}, {"n", 1}}), Header("content-type", "text/json"))
	text := use.message.GetHttp().GetText().GetParts()
	if len(text) != 3 || text[0].GetLiteral() != `{"token":"` || text[1].GetRef().GetEncoding() != pb.RefEncoding_REF_ENCODING_JSON_STRING ||
		text[2].GetLiteral() != `","n":1}` || len(use.message.GetHttp().GetHeaders()) != 1 {
		t.Fatalf("use %s", stepJSON(t, use))
	}
	if got := Put("/x", Body([]byte("v")), Expect(200)).String(); got != "Step(PUT /x)" {
		t.Fatalf("String %s", got)
	}
}

func TestBadStepsAndRequestsSayWhatToFix(t *testing.T) {
	for want, build := range map[string]func(){
		"must start with a literal": func() { Get("kv") },
		"at most one of":            func() { Post("/x", Body(nil), JSON(1)) },
		"not a JSON pointer":        func() { Get("/", Capture("t", "token")) },
		"must match":                func() { Ref("Bad") },
		"100..599":                  func() { Get("/", Expect(700)) },
		"is not JSON":               func() { Post("/", JSON(math.NaN())) },
	} {
		err := safely("build", build)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: %v", want, err)
		}
	}
	_, err := planned(Request{Steps: []Step{Get("/", Header("A", Ref("token")))}}, 0, false)
	if err == nil || !strings.Contains(err.Error(), "no earlier step captures") {
		t.Fatalf("%v", err)
	}
	if _, err := planned(Request{}, 0, false); err == nil {
		t.Fatal("an empty request was planned")
	}
}
