package jobenv_test

import (
	"encoding/json"
	"errors"
	"os"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/jobenv"
)

// The corpus is the contract with vibesys-cli's codec, which reads and writes it too.
const corpusFile = "../../../../../proto/vibesys/cli/v1/conformance/environment.json"

func variablesOf(err error) []string {
	var out []string
	var walk func(error)
	walk = func(e error) {
		if joined, ok := e.(interface{ Unwrap() []error }); ok {
			for _, inner := range joined.Unwrap() {
				walk(inner)
			}
			return
		}
		var named *jobenv.Error
		if errors.As(e, &named) {
			out = append(out, named.Variable)
		}
	}
	walk(err)
	slices.Sort(out)
	return slices.Compact(out)
}

// Reading the variables of each case of the corpus gives the message, or refuses the variables,
// that the corpus says; and no error repeats a value.
func TestTheCorpusIsWhatTheReaderDoes(t *testing.T) {
	data, err := os.ReadFile(corpusFile)
	if err != nil {
		t.Fatal(err)
	}
	var corpus struct {
		Cases []struct {
			Name      string            `json:"name"`
			Message   string            `json:"message"`
			Variables map[string]string `json:"variables"`
			Decoded   json.RawMessage   `json:"decoded"`
			Error     []string          `json:"error"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(data, &corpus); err != nil {
		t.Fatal(err)
	}
	if len(corpus.Cases) == 0 {
		t.Fatal("the corpus has no case")
	}
	for _, c := range corpus.Cases {
		t.Run(c.Name, func(t *testing.T) {
			var got, want proto.Message = &cliv1.JobEnvironment{}, &cliv1.JobEnvironment{}
			if c.Message == "KubernetesEnvironment" {
				got, want = &cliv1.KubernetesEnvironment{}, &cliv1.KubernetesEnvironment{}
			}
			err := jobenv.Decode(func(name string) string { return c.Variables[name] }, got)
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
			if err := protojson.Unmarshal(c.Decoded, want); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(got, want) {
				t.Fatalf("read %v, want %v", got, want)
			}
		})
	}
}

// A person told a variable is missing is told its name, from the message and not from a copy.
func TestAFieldKnowsItsVariable(t *testing.T) {
	if got := jobenv.NameOf(&cliv1.JobEnvironment{}, "timeout_seconds"); got != "VIBESYS_JOB_TIMEOUT_SECONDS" {
		t.Fatalf("got %q", got)
	}
	if got := jobenv.NameOf(&cliv1.JobEnvironment{}, "nosuch"); got != "" {
		t.Fatalf("got %q", got)
	}
}
