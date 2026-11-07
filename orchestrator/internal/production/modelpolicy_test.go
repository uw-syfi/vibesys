package production_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

// holderRunner is the program that holds a session, as far as `catalog` goes: it answers with what it
// is told to, and remembers how it was asked.
type holderRunner struct {
	answer string
	err    error
	asked  [][]string
}

func (h *holderRunner) Run(_ context.Context, program string, args, _ []string) ([]byte, []byte, error) {
	h.asked = append(h.asked, append([]string{program}, args...))
	return []byte(h.answer), []byte("it said why"), h.err
}

const catalogAnswer = `{"harnesses":[{"harness":"HARNESS_CODEX","models":["model-a","model-b"]},{"harness":"HARNESS_CLAUDE_CODE","models":["model-c"]}]}`

func holderParts() production.Parts {
	return production.Parts{Config: &pb.HostConfig{SessionHolderCommand: "/opt/holder run --quiet"}}
}

func writePolicy(t *testing.T, home, text string) {
	t.Helper()
	path := hostconfig.HostSettingsPath(home)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

// The catalog is asked of the program that holds a session, once, and what it says is each harness's
// models in its order.
func TestTheCatalogIsAskedOfTheSessionHolderOnce(t *testing.T) {
	runner := &holderRunner{answer: catalogAnswer}
	catalog := &production.SidecarCatalog{Parts: holderParts(), Runner: runner}
	for range 2 {
		known, err := catalog.Models(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		if !slices.Equal(known[core.Codex], []core.ModelName{"model-a", "model-b"}) || !slices.Equal(known[core.ClaudeCode], []core.ModelName{"model-c"}) {
			t.Fatalf("known %v, want what the holder said", known)
		}
	}
	if want := [][]string{{"/opt/holder", "run", "--quiet", "catalog"}}; len(runner.asked) != 1 || !slices.Equal(runner.asked[0], want[0]) {
		t.Fatalf("asked %v, want %v once", runner.asked, want)
	}
}

// An answer that is not a catalog, or a program that fails, is an error that is not remembered: the
// next ask tries again.
func TestACatalogThatCannotBeReadIsAnErrorAndAsksAgain(t *testing.T) {
	runner := &holderRunner{err: errors.New("exit status 1")}
	catalog := &production.SidecarCatalog{Parts: holderParts(), Runner: runner}
	if _, err := catalog.Models(context.Background()); err == nil {
		t.Fatal("a failing program gave a catalog")
	}
	runner.err, runner.answer = nil, "not json"
	if _, err := catalog.Models(context.Background()); err == nil {
		t.Fatal("an answer that is not a catalog was taken")
	}
	runner.answer = catalogAnswer
	if _, err := catalog.Models(context.Background()); err != nil {
		t.Fatalf("the third ask failed: %v", err)
	}
	if len(runner.asked) != 3 {
		t.Fatalf("asked %d times, want each ask to try", len(runner.asked))
	}
}

// The policy is the settings file as it is now, with the catalog beside it: a missing file restricts
// nothing, a listed model is allowed, an edit counts at the next look, and a file that cannot be read
// is a closed policy and never an empty one.
func TestTheHostPolicyIsTheSettingsFileAsItIsNow(t *testing.T) {
	home := t.TempDir()
	runner := &holderRunner{answer: catalogAnswer}
	models := production.HostModels{Home: home, Catalog: &production.SidecarCatalog{Parts: holderParts(), Runner: runner}}

	if p := models.ModelPolicy(); p.Restricts() {
		t.Fatalf("no file gave %+v, want no restriction", p)
	}
	writePolicy(t, home, "allowed_models = [\"model-b\"]\n")
	p := models.ModelPolicy()
	if !slices.Equal(p.Allowed, []core.ModelName{"model-b"}) || p.Closed || !slices.Equal(p.Known[core.Codex], []core.ModelName{"model-a", "model-b"}) {
		t.Fatalf("policy %+v, want model-b allowed with the catalog beside it", p)
	}
	if got, ok := p.Resolve(core.Codex, ""); !ok || got != "model-b" {
		t.Fatalf("an unpinned codex resolves to %q, %v; want model-b", got, ok)
	}
	writePolicy(t, home, "allowed_models = [\n")
	if p := models.ModelPolicy(); !p.Closed || !p.Restricts() {
		t.Fatalf("an unreadable file gave %+v, want a closed policy", p)
	}
}

// A catalog that cannot be had leaves an unpinned model with nothing to resolve to, so a host that
// restricts models refuses it rather than letting the harness pick.
func TestAHostThatRestrictsModelsRefusesAnUnpinnedModelWhenTheCatalogIsGone(t *testing.T) {
	home := t.TempDir()
	writePolicy(t, home, "allowed_models = [\"model-a\"]\n")
	runner := &holderRunner{err: errors.New("exit status 1")}
	p := production.HostModels{Home: home, Catalog: &production.SidecarCatalog{Parts: holderParts(), Runner: runner}}.ModelPolicy()
	if got, ok := p.Resolve(core.Codex, ""); ok {
		t.Fatalf("an unpinned codex resolved to %q with no catalog", got)
	}
	if got, ok := p.Resolve(core.Codex, "model-a"); !ok || got != "model-a" {
		t.Fatalf("a pinned allowed model gave %q, %v", got, ok)
	}
}
