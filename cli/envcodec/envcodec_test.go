package envcodec_test

import (
	"errors"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// word is a random string of characters a variable can hold.
func word(r *rand.Rand, alphabet string, max int) string {
	letters := []rune(alphabet)
	out := make([]rune, r.IntN(max+1))
	for i := range out {
		out[i] = letters[r.IntN(len(letters))]
	}
	return string(out)
}

const anyText = "abcXYZ019-_. /:\"'$\\é\t"

func anyAgentContext(r *rand.Rand) *cliv1.AgentContext {
	return &cliv1.AgentContext{
		Repository: word(r, anyText, 40), Task: word(r, anyText, 20), RunId: word(r, anyText, 20),
		Harness: word(r, "abcz09-", 12), Agent: word(r, anyText, 20),
	}
}

func anySkillEnvironment(r *rand.Rand) *cliv1.SkillEnvironment {
	if r.IntN(3) == 0 {
		return &cliv1.SkillEnvironment{}
	}
	return &cliv1.SkillEnvironment{Cli: "/" + word(r, anyText, 40)}
}

func anyOperatorContext(r *rand.Rand) *cliv1.OperatorContext {
	return &cliv1.OperatorContext{Placement: word(r, anyText, 20)}
}

func lookupOf(set map[string]string) envcodec.Lookup {
	return func(name string) string { return set[name] }
}

// Whatever a message holds, what Encode writes decodes to an equal message, Set and Unset together
// are exactly the variables the message declares, and a field is unset exactly when it is empty.
func TestAMessageDecodesToWhatItEncodedFrom(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 11))
	for range 3000 {
		for _, m := range []proto.Message{anyAgentContext(r), anySkillEnvironment(r), anyOperatorContext(r)} {
			encoded, err := envcodec.Encode(m)
			if err != nil {
				t.Fatalf("%v: %v", m, err)
			}
			names, err := envcodec.Names(m)
			if err != nil {
				t.Fatal(err)
			}
			var accounted []string
			for name := range encoded.Set {
				accounted = append(accounted, name)
			}
			accounted = append(accounted, encoded.Unset...)
			slices.Sort(accounted)
			if want := slices.Sorted(slices.Values(names)); !slices.Equal(accounted, want) {
				t.Fatalf("%v: set and unset are %v, the message declares %v", m, accounted, want)
			}
			for name, value := range encoded.Set {
				if value == "" {
					t.Fatalf("%v: %s is set to the empty string", m, name)
				}
			}
			got := m.ProtoReflect().New().Interface()
			if err := envcodec.Decode(lookupOf(encoded.Set), got); err != nil {
				t.Fatalf("%v encoded as %v: %v", m, encoded.Set, err)
			}
			if !proto.Equal(got, m) {
				t.Fatalf("%v encoded as %v decoded as %v", m, encoded.Set, got)
			}
		}
	}
}

// Every name is declared by exactly one field of the messages a process reads, so a name has one
// definition.
func TestEveryNameIsDeclaredOnce(t *testing.T) {
	seen := map[string]bool{}
	for _, m := range []proto.Message{&cliv1.AgentContext{}, &cliv1.OperatorContext{}, &cliv1.SkillEnvironment{}} {
		names, err := envcodec.Names(m)
		if err != nil {
			t.Fatal(err)
		}
		for _, name := range names {
			if seen[name] || !strings.HasPrefix(name, "VIBESYS_") {
				t.Errorf("%s is declared twice or is not a VIBESYS_ name", name)
			}
			seen[name] = true
		}
	}
	// The names are the ones the agents' skills and the job tool have always read.
	for _, name := range []string{"VIBESYS_REPO", "VIBESYS_TASK", "VIBESYS_RUN", "VIBESYS_AGENT", "VIBESYS_HARNESS", "VIBESYS_PLACEMENT", "VIBESYS_CLI"} {
		if !seen[name] {
			t.Errorf("%s is not declared", name)
		}
	}
}

// A value that breaks a rule is refused with an error that names its variable, whatever the value,
// and that never repeats the value.
func TestABadValueIsRefusedNamingItsVariable(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 5))
	for range 2000 {
		bad := "X" + word(r, "ABC xyz!", 10) // an upper-case or odd character no harness has.
		var harness cliv1.AgentContext
		err := envcodec.Decode(lookupOf(map[string]string{"VIBESYS_HARNESS": bad}), &harness)
		var named *envcodec.Error
		if !errors.As(err, &named) || named.Variable != "VIBESYS_HARNESS" || strings.Contains(err.Error(), bad) {
			t.Fatalf("harness %q: got %v", bad, err)
		}
		var skills cliv1.SkillEnvironment
		relative := word(r, "abc", 8) + "x"
		err = envcodec.Decode(lookupOf(map[string]string{"VIBESYS_CLI": relative}), &skills)
		if !errors.As(err, &named) || named.Variable != "VIBESYS_CLI" {
			t.Fatalf("cli %q: got %v", relative, err)
		}
	}
}

// Several bad values are all reported, each naming its variable, so one run of the program tells a
// person everything to fix.
func TestEveryBadValueIsReported(t *testing.T) {
	var m cliv1.AgentContext
	err := envcodec.Decode(lookupOf(map[string]string{"VIBESYS_HARNESS": "Bad", "VIBESYS_TASK": strings.Repeat("t", 300)}), &m)
	if err == nil || !strings.Contains(err.Error(), "VIBESYS_HARNESS") || !strings.Contains(err.Error(), "VIBESYS_TASK") {
		t.Fatalf("got %v, want both variables named", err)
	}
}

// A message that declares no namespace has no unknown names, whatever the environment holds.
func TestNoNamespaceMeansNoUnknownNames(t *testing.T) {
	if got := envcodec.Unknown([]string{"VIBESYS_NOT_A_NAME=1", "VIBESYS_TASK=t"}, &cliv1.AgentContext{}); got != nil {
		t.Fatalf("got %v", got)
	}
}

// FromEnviron reads the form os.Environ returns: the last of a repeated name wins, an entry with no
// equals sign is no variable, and a value may hold equals signs.
func TestFromEnvironReadsOsEnvironForm(t *testing.T) {
	lookup := envcodec.FromEnviron([]string{"A=1", "B=x=y", "A=2", "C"})
	for name, want := range map[string]string{"A": "2", "B": "x=y", "C": "", "D": ""} {
		if got := lookup(name); got != want {
			t.Errorf("%s is %q, want %q", name, got, want)
		}
	}
}
