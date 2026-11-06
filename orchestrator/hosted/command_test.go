package hosted

import (
	"errors"
	"math/rand/v2"
	"slices"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

func word(r *rand.Rand) string {
	letters := "abcxyz019-_"
	out := []byte{letters[r.IntN(len(letters))]}
	for range r.IntN(20) {
		out = append(out, letters[r.IntN(len(letters))])
	}
	return string(out)
}

func anyCredentials(r *rand.Rand) *pb.ToolServerCredentials {
	return &pb.ToolServerCredentials{
		Endpoint: "/" + word(r), Agent: word(r), Session: 1 + r.Uint32N(1<<20), Token: word(r), Run: word(r),
		Role: pb.AgentRole(1 + r.IntN(6)),
	}
}

func environOf(t *testing.T, m proto.Message) []string {
	t.Helper()
	encoded, err := envcodec.Encode(m)
	if err != nil {
		t.Fatal(err)
	}
	var environ []string
	for name, value := range encoded.Set {
		environ = append(environ, name+"="+value)
	}
	slices.Sort(environ)
	return environ
}

// What the daemon gives a session's tool server is what the tool server reads, for every session
// number and every role, and no name beside its own is reported.
func TestTheToolServerReadsWhatItWasGiven(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	for range 2000 {
		given := anyCredentials(r)
		environ := append(environOf(t, given), "PATH=/usr/bin", "VIBESYS_RUN=r", "HOME=/h")
		got, unknown, err := credentialsFrom(environ)
		if err != nil || !proto.Equal(got, given) || len(unknown) != 0 {
			t.Fatalf("%v read as %v, %v, %v", given, got, unknown, err)
		}
	}
}

// Each variable of the credentials is required, and one that is missing or breaks its rule is
// refused with an error that names that variable and never repeats the value, the token included.
func TestACredentialThatIsMissingOrBadIsRefusedNamingItsVariable(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	names, err := envcodec.Names(&pb.ToolServerCredentials{})
	if err != nil {
		t.Fatal(err)
	}
	for range 500 {
		given := anyCredentials(r)
		given.Token = "secret-" + given.Token // a string no message of the codec contains.
		for _, spoil := range []struct {
			name  string
			value string
		}{
			{envcodec.NameOf(given, "session"), "not-a-number-" + word(r)},
			{envcodec.NameOf(given, "session"), "0"},
			{envcodec.NameOf(given, "session"), strconv.FormatUint(1<<32, 10)},
			{envcodec.NameOf(given, "role"), "boss"},
			{envcodec.NameOf(given, "role"), "unspecified"},
			{envcodec.NameOf(given, "endpoint"), "relative/" + word(r)},
		} {
			environ := withValue(environOf(t, given), spoil.name, spoil.value)
			_, _, err := credentialsFrom(environ)
			var named *envcodec.Error
			if !errors.As(err, &named) || named.Variable != spoil.name {
				t.Fatalf("%s=%q: got %v", spoil.name, spoil.value, err)
			}
			if strings.Contains(err.Error(), given.GetToken()) {
				t.Fatalf("the error repeats the token: %v", err)
			}
		}
		for _, name := range names {
			_, _, err := credentialsFrom(withValue(environOf(t, given), name, ""))
			var named *envcodec.Error
			if !errors.As(err, &named) || named.Variable != name {
				t.Fatalf("%s unset: got %v", name, err)
			}
		}
	}
}

func withValue(environ []string, name, value string) []string {
	out := slices.DeleteFunc(slices.Clone(environ), func(entry string) bool { return strings.HasPrefix(entry, name+"=") })
	if value == "" {
		return out
	}
	return append(out, name+"="+value)
}

// A name in the credentials' namespace that no field reads is reported and not refused: the
// environment is inherited, and a name in that prefix can be a setting of another program. A name
// outside the namespace is never reported. The choice follows the rule that unknown keys are
// reported to the person who can fix them, without breaking a session that has what it needs.
func TestANameInTheNamespaceNoFieldReadsIsReportedNotRefused(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	for range 500 {
		given := anyCredentials(r)
		stray := "VIBESYS_ORCHESTRATOR_" + strings.ToUpper(word(r))
		if slices.Contains(mustNames(t), stray) {
			continue
		}
		environ := append(environOf(t, given), stray+"=1", "VIBESYS_"+strings.ToUpper(word(r))+"=1")
		got, unknown, err := credentialsFrom(environ)
		if err != nil || !proto.Equal(got, given) || !slices.Equal(unknown, []string{stray}) {
			t.Fatalf("%v with %s: read as %v, unknown %v, %v", given, stray, got, unknown, err)
		}
	}
}

func mustNames(t *testing.T) []string {
	t.Helper()
	names, err := envcodec.Names(&pb.ToolServerCredentials{})
	if err != nil {
		t.Fatal(err)
	}
	return names
}

// The names are the ones the tool server has always been launched with.
func TestTheCredentialsKeepTheirVariableNames(t *testing.T) {
	want := []string{
		"VIBESYS_ORCHESTRATOR_SOCKET", "VIBESYS_ORCHESTRATOR_AGENT", "VIBESYS_ORCHESTRATOR_SESSION",
		"VIBESYS_ORCHESTRATOR_TOKEN", "VIBESYS_ORCHESTRATOR_RUN", "VIBESYS_ORCHESTRATOR_ROLE",
	}
	if got := mustNames(t); !slices.Equal(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
