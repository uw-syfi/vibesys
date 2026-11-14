package production

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// operatorService opens a daemon whose catalogue and provisioner are the release's, as the host wires
// them, and serves the operator's side of it.
func operatorService(t *testing.T, find release.Finder, config *pb.HostConfig, home string) *daemon.Service {
	t.Helper()
	layout, err := LayoutFor(t.TempDir(), "/unused")
	if err != nil {
		t.Skipf("the test directory is not a local filesystem: %v", err)
	}
	catalogue, provisioner := ReleaseExampleSources(find, config, home)
	d, err := Open(layout, Wiring{
		Sidecars: NoSidecars{}, Parts: readyParts{}, Tree: fakeTree{}, Version: "1.0.0",
		Catalogue: catalogue, Provisioner: provisioner,
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = d.Close() })
	service, err := d.Service(daemon.AudienceOperator)
	if err != nil {
		t.Fatal(err)
	}
	return service
}

// A host over a built release serves the release's catalogue with the host's repositories directory,
// and provisions an example into that directory by default, into the request's parent when one is
// sent, and again to the same place for the same request.
func TestAHostOverABuiltReleaseServesItsCatalogueAndProvisionsItsExamples(t *testing.T) {
	home := t.TempDir()
	service := operatorService(t, builtRelease(t), &pb.HostConfig{}, home)

	listed, err := service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{})
	if err != nil {
		t.Fatal(err)
	}
	defaultDir := filepath.Join(home, ".vibesys", "repos")
	if listed.GetRelease() != "1.0.0" || listed.GetRepositoriesDirectory() != defaultDir || len(listed.GetEntries()) != 3 {
		t.Fatalf("catalogue: release %q, directory %q, %d entries", listed.GetRelease(), listed.GetRepositoriesDirectory(), len(listed.GetEntries()))
	}

	example := &pb.ProvisionRepositoryRequest{
		RequestId: "r1", Name: "mine", Source: &pb.ProvisionRepositoryRequest_Example{Example: "db/kv"},
	}
	res, err := service.ProvisionRepository(context.Background(), example)
	if err != nil || res.GetProvisioned() == nil {
		t.Fatalf("provision: %v, %v", res, err)
	}
	if want := filepath.Join(defaultDir, "mine"); res.GetProvisioned().GetRepository() != want {
		t.Fatalf("repository %q, want %q", res.GetProvisioned().GetRepository(), want)
	}
	if _, err := os.Stat(filepath.Join(defaultDir, "mine", "src", "lib.rs")); err != nil {
		t.Fatalf("the example's files are not in the repository: %v", err)
	}
	again, err := service.ProvisionRepository(context.Background(), example)
	if err != nil || again.GetProvisioned().GetCommit() != res.GetProvisioned().GetCommit() {
		t.Fatalf("the same request again: %v, %v", again, err)
	}

	elsewhere := filepath.Join(home, "elsewhere")
	sent := &pb.ProvisionRepositoryRequest{
		RequestId: "r2", Name: "mine", ParentDirectory: elsewhere, Source: &pb.ProvisionRepositoryRequest_Example{Example: "db/kv"},
	}
	if res, err := service.ProvisionRepository(context.Background(), sent); err != nil || res.GetProvisioned().GetRepository() != filepath.Join(elsewhere, "mine") {
		t.Fatalf("a request's own parent: %v, %v", res, err)
	}
	missing := &pb.ProvisionRepositoryRequest{RequestId: "r3", Name: "other", Source: &pb.ProvisionRepositoryRequest_Example{Example: "db/nothing"}}
	if res, err := service.ProvisionRepository(context.Background(), missing); err != nil || res.GetRefused().GetReason() != pb.ProvisionRefusal_PROVISION_REFUSAL_EXAMPLE_NOT_FOUND {
		t.Fatalf("an example the release lacks: %v, %v", res, err)
	}
}

// The configured repositories directory is the default parent and is what the catalogue shows; "~" in
// it is the host user's home.
func TestTheConfiguredRepositoriesDirectoryIsTheDefaultParent(t *testing.T) {
	home := t.TempDir()
	service := operatorService(t, builtRelease(t), &pb.HostConfig{RepositoriesDirectory: "~/work"}, home)
	listed, err := service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{})
	if err != nil || listed.GetRepositoriesDirectory() != filepath.Join(home, "work") {
		t.Fatalf("catalogue: %v, %v", listed, err)
	}
	empty := &pb.ProvisionRepositoryRequest{RequestId: "r", Name: "fresh", Source: &pb.ProvisionRepositoryRequest_Empty{Empty: &pb.EmptyRepository{}}}
	if res, err := service.ProvisionRepository(context.Background(), empty); err != nil || res.GetProvisioned().GetRepository() != filepath.Join(home, "work", "fresh") {
		t.Fatalf("provision: %v, %v", res, err)
	}
}

// A host with no release (a developer's build) answers ListCatalogue not with a failure but with a
// classified problem naming the file it lacks, never crashes, and still makes an empty repository.
func TestAHostWithoutAReleaseAnswersTheCatalogueProblemNamingTheFile(t *testing.T) {
	home := t.TempDir()
	service := operatorService(t, developerBuild, &pb.HostConfig{}, home)
	resp, err := service.ListCatalogue(context.Background(), &pb.ListCatalogueRequest{})
	if err != nil {
		t.Fatalf("list catalogue of a developer build: %v", err)
	}
	if got := resp.GetProblem().GetReason(); got != pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_NO_RELEASE {
		t.Fatalf("problem reason = %v, want NO_RELEASE", got)
	}
	if detail := resp.GetProblem().GetDetail(); !strings.Contains(detail, release.ManifestName) {
		t.Fatalf("problem detail %q does not name %s", detail, release.ManifestName)
	}
	empty := &pb.ProvisionRepositoryRequest{RequestId: "r", Name: "fresh", Source: &pb.ProvisionRepositoryRequest_Empty{Empty: &pb.EmptyRepository{}}}
	if res, err := service.ProvisionRepository(context.Background(), empty); err != nil || res.GetProvisioned() == nil {
		t.Fatalf("an empty repository: %v, %v", res, err)
	}
}

// expired is Bounds whose bound has already run out.
type expired struct{}

func (expired) Bound(ctx context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(ctx)
	cancel()
	return ctx, cancel
}

// A command runs under the context Bounds gives it: one whose bound is gone does not run.
func TestACommandRunsUnderItsBound(t *testing.T) {
	dir := t.TempDir()
	if _, err := (ExecGit{Bounds: expired{}}).Run(context.Background(), dir, "init"); err == nil {
		t.Fatal("a command ran past its bound")
	}
	if entries, err := os.ReadDir(dir); err != nil || len(entries) != 0 {
		t.Fatalf("the command left %v, %v", entries, err)
	}
	if _, err := (ExecGit{Bounds: GitTimeouts{}}).Run(context.Background(), dir, "init"); err != nil {
		t.Fatalf("a command within its bound: %v", err)
	}
}
