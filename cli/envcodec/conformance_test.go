package envcodec_test

import (
	"encoding/json"
	"errors"
	"math/rand/v2"
	"os"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// corpusFile is read by the Python codec of the vibesys-evaluator package as well.
const corpusFile = "../../proto/vibesys/cli/v1/conformance/environment.json"

type corpusCase struct {
	Name      string            `json:"name"`
	Message   string            `json:"message"`
	Variables map[string]string `json:"variables"`
	Decoded   json.RawMessage   `json:"decoded"`
	Writes    map[string]string `json:"writes"`
	Error     []string          `json:"error"`
}

func newMessage(t *testing.T, name string) proto.Message {
	t.Helper()
	switch name {
	case "JobEnvironment":
		return &cliv1.JobEnvironment{}
	case "KubernetesEnvironment":
		return &cliv1.KubernetesEnvironment{}
	}
	t.Fatalf("the corpus names a message %q this test does not know", name)
	return nil
}

func variablesOf(err error) []string {
	var out []string
	for _, e := range flatten(err) {
		var named *envcodec.Error
		if errors.As(e, &named) {
			out = append(out, named.Variable)
		}
	}
	slices.Sort(out)
	return slices.Compact(out)
}

func flatten(err error) []error {
	if joined, ok := err.(interface{ Unwrap() []error }); ok {
		var out []error
		for _, e := range joined.Unwrap() {
			out = append(out, flatten(e)...)
		}
		return out
	}
	return []error{err}
}

// Reading the variables of a case gives the message, or refuses the variables, that the corpus says,
// and writing the message gives the variables back.
func TestTheCorpusIsWhatTheCodecDoes(t *testing.T) {
	data, err := os.ReadFile(corpusFile)
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct {
		Cases []corpusCase `json:"cases"`
	}
	if err := json.Unmarshal(data, &corpus); err != nil {
		t.Fatal(err)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("the corpus has no case")
	}
	for _, c := range corpus.Cases {
		t.Run(c.Name, func(t *testing.T) {
			got := newMessage(t, c.Message)
			err := envcodec.Decode(func(name string) string { return c.Variables[name] }, got)
			if c.Error != nil {
				if err == nil {
					t.Fatalf("read %v, want it refused", got)
				}
				if names := variablesOf(err); !slices.Equal(names, c.Error) {
					t.Fatalf("refused %v (%v), want %v", names, err, c.Error)
				}
				for _, value := range c.Variables {
					if len(value) > 5 && strings.Contains(err.Error(), value) {
						t.Fatalf("the error repeats a value: %v", err)
					}
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			want := newMessage(t, c.Message)
			if err := protojson.Unmarshal(c.Decoded, want); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(got, want) {
				t.Fatalf("read %v, want %v", got, want)
			}
			written, err := envcodec.Encode(want)
			if err != nil {
				t.Fatal(err)
			}
			expected := c.Writes
			if expected == nil {
				expected = c.Variables
			}
			if !mapsEqual(written.Set, expected) {
				t.Fatalf("wrote %v, want %v", written.Set, expected)
			}
		})
	}
}

func mapsEqual(a, b map[string]string) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

func anyMountName(r *rand.Rand) string {
	return "a" + word(r, "abcz09_-", 20)
}

func anyJobEnvironment(r *rand.Rand) *cliv1.JobEnvironment {
	m := &cliv1.JobEnvironment{}
	if r.IntN(2) == 0 {
		m.Artifacts = "/" + word(r, anyText, 40)
	}
	if r.IntN(2) == 0 {
		m.Cache = "/" + word(r, anyText, 40)
	}
	if r.IntN(2) == 0 {
		m.ServiceUrl = "http://" + word(r, "abc09.:", 30) + "x"
	}
	if r.IntN(2) == 0 {
		m.TimeoutSeconds = uint32(r.IntN(31536001))
	}
	seen := map[string]bool{}
	for range r.IntN(5) {
		if name := anyMountName(r); !seen[name] && name != "" {
			seen[name] = true
			m.Mounts = append(m.Mounts, name)
		}
	}
	return m
}

// Any valid job environment is written as variables that read back as the same message.
func TestAnyJobEnvironmentRoundTrips(t *testing.T) {
	r := rand.New(rand.NewPCG(13, 17))
	for range 3000 {
		m := anyJobEnvironment(r)
		written, err := envcodec.Encode(m)
		if err != nil {
			t.Fatalf("%v: %v", m, err)
		}
		got := &cliv1.JobEnvironment{}
		if err := envcodec.Decode(func(name string) string { return written.Set[name] }, got); err != nil {
			t.Fatalf("%v written as %v: %v", m, written.Set, err)
		}
		if !proto.Equal(got, m) {
			t.Fatalf("%v written as %v read as %v", m, written.Set, got)
		}
	}
}

// A bad value of any variable is refused with an error that names that variable and not the value.
func TestABadJobVariableIsNamed(t *testing.T) {
	r := rand.New(rand.NewPCG(19, 23))
	bad := map[string]func() string{
		"VIBESYS_ARTIFACTS_DIR":       func() string { return "qzxqzxq" + word(r, "abc/", 12) },
		"VIBESYS_CACHE_DIR":           func() string { return "qzxqzxq" + word(r, "abc/", 12) },
		"VIBESYS_SERVICE_URL":         func() string { return "qzxqzxq" + word(r, "abc :/", 12) },
		"VIBESYS_JOB_TIMEOUT_SECONDS": func() string { return "qzxqzxq" + word(r, "abc09", 12) },
		"VIBESYS_MOUNTS":              func() string { return "QzxqzxQ" + word(r, "abc,", 12) },
	}
	for range 500 {
		for name, value := range bad {
			text := value()
			err := envcodec.Decode(func(n string) string {
				if n == name {
					return text
				}
				return ""
			}, &cliv1.JobEnvironment{})
			if names := variablesOf(err); !slices.Equal(names, []string{name}) || strings.Contains(err.Error(), text) {
				t.Fatalf("%s=%q: refused %v: %v", name, text, names, err)
			}
		}
	}
}
