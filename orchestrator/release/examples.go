package release

import (
	"errors"
	"fmt"
	"io/fs"
	"path"
	"slices"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The example tasks a release carries. RELEASE/catalogue.pb is a ReleaseCatalogue and says what is
// in RELEASE/examples/, which holds one folder for each example, at the example's id
// ("model-serving/flint" is RELEASE/examples/model-serving/flint). An example that is cloned from
// elsewhere has only its own files in its folder, and the catalogue pins the clone.
const (
	// ExamplesRoot is the release's folder of example trees.
	ExamplesRoot = "examples"
	// CatalogueName is the catalogue's file name, in the release folder.
	CatalogueName = "catalogue.pb"
)

// ParseCatalogue reads catalogue.pb, refusing bytes that are not a ReleaseCatalogue and any value
// its rules reject.
func ParseCatalogue(data []byte) (*pb.ReleaseCatalogue, error) {
	var c pb.ReleaseCatalogue
	if err := proto.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("not a release catalogue: %w", err)
	}
	if err := protovalidate.Validate(&c); err != nil {
		return nil, fmt.Errorf("not a valid release catalogue: %w", err)
	}
	return &c, nil
}

// MarshalCatalogue writes the catalogue as the file the release holds, after checking it. Equal
// catalogues give equal bytes.
func MarshalCatalogue(c *pb.ReleaseCatalogue) ([]byte, error) {
	if err := protovalidate.Validate(c); err != nil {
		return nil, fmt.Errorf("not a valid release catalogue: %w", err)
	}
	return proto.MarshalOptions{Deterministic: true}.Marshal(c)
}

// Catalogue is the release's catalogue. It is an *IncompleteError naming the file when it is
// missing, and any other error when the file is not a valid catalogue.
func (r *Release) Catalogue() (*pb.ReleaseCatalogue, error) {
	data, err := fs.ReadFile(r.fsys, rooted(r.abs(CatalogueName)))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, &IncompleteError{Dir: r.Dir, Missing: []string{CatalogueName}}
	}
	if err != nil {
		return nil, err
	}
	c, err := ParseCatalogue(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", r.abs(CatalogueName), err)
	}
	if c.GetRelease() != r.Version() {
		return nil, fmt.Errorf("%s: built for release %s, and this is release %s", r.abs(CatalogueName), c.GetRelease(), r.Version())
	}
	return c, nil
}

// CloneSpec names a tree that lives in another repository: where it is cloned from and the revision
// it is pinned to.
type CloneSpec struct {
	URL      string
	Revision string
}

// ExampleSource is what an example is made of: its own files, rooted at the example's folder, and
// the repository the rest of it is cloned from. Clone is nil when the example is all in Vendored.
type ExampleSource struct {
	Vendored fs.FS
	Clone    *CloneSpec
}

// ExampleNotFoundError says the release's catalogue lists no such example.
type ExampleNotFoundError struct{ Example string }

func (e *ExampleNotFoundError) Error() string {
	return fmt.Sprintf("example %q is not in this release", e.Example)
}

// ExampleSource is the source of the example with the id. The id must be one the catalogue lists,
// so a name that climbs out of the examples folder is simply not found. It is an
// *ExampleNotFoundError for an id the catalogue does not list, and an *IncompleteError naming the
// folder when the release lacks the example's files.
func (r *Release) ExampleSource(example string) (ExampleSource, error) {
	c, err := r.Catalogue()
	if err != nil {
		return ExampleSource{}, err
	}
	return r.sourceIn(c, example)
}

func (r *Release) sourceIn(c *pb.ReleaseCatalogue, example string) (ExampleSource, error) {
	i := slices.IndexFunc(c.GetExamples(), func(e *pb.ReleaseExample) bool { return e.GetExample() == example })
	if i < 0 {
		return ExampleSource{}, &ExampleNotFoundError{Example: example}
	}
	e := c.GetExamples()[i]
	dir := rooted(r.abs(e.GetTree()))
	if info, err := fs.Stat(r.fsys, dir); err != nil || !info.IsDir() {
		return ExampleSource{}, &IncompleteError{Dir: r.Dir, Missing: []string{e.GetTree()}}
	}
	tree, err := fs.Sub(r.fsys, dir)
	if err != nil {
		return ExampleSource{}, err
	}
	source := ExampleSource{Vendored: tree}
	if cl := e.GetClone(); cl != nil {
		source.Clone = &CloneSpec{URL: cl.GetUrl(), Revision: cl.GetRevision()}
	}
	return source, nil
}

// missingExamples are the trees the catalogue names that the release does not hold, relative to the
// release.
func (r *Release) missingExamples(c *pb.ReleaseCatalogue) []string {
	var missing []string
	for _, e := range c.GetExamples() {
		if info, err := fs.Stat(r.fsys, rooted(r.abs(e.GetTree()))); err != nil || !info.IsDir() {
			missing = append(missing, path.Clean(e.GetTree()))
		}
	}
	return missing
}
