package assemble

import (
	"context"
	"fmt"
	"path"
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// CardReader is the CLI's reader of a task's card: the one reader of task files, so the catalogue
// says what a run started on the task would be told. folder is the absolute path of an example's
// folder in the stage; the card's task is the one named.
type CardReader interface {
	Card(ctx context.Context, folder, task string) (*cliv1.TaskCard, error)
}

// Catalogue is the catalogue of the examples laid into the stage at stageDir (absolute), one entry
// for each task of each example, ordered by example id and task name. A task whose card cannot be
// read fails the build, naming the example and the task: the release does not ship a task the CLI
// cannot read. The result is valid by the catalogue's rules, or it is an error.
func Catalogue(ctx context.Context, version, stageDir string, staged []Staged, cards CardReader) (*pb.ReleaseCatalogue, error) {
	c := &pb.ReleaseCatalogue{Release: version}
	for _, s := range staged {
		for _, task := range s.Tasks {
			folder := path.Join(stageDir, release.ExamplesRoot, s.ID())
			card, err := cards.Card(ctx, folder, task)
			if err != nil {
				return nil, fmt.Errorf("example %s, task %s: %w", s.ID(), task, err)
			}
			if card.GetTask() != task {
				return nil, fmt.Errorf("example %s, task %s: the card is of task %q", s.ID(), task, card.GetTask())
			}
			e := &pb.ReleaseExample{
				Example: s.ID(),
				Folder:  path.Base(s.ID()),
				Tree:    path.Join(release.ExamplesRoot, s.ID()),
				Card:    card,
			}
			if s.Pin != nil {
				e.NeedsClone = true
				e.Clone = &pb.CloneSource{Url: s.Pin.URL, Revision: s.Pin.Revision}
			}
			c.Examples = append(c.Examples, e)
		}
	}
	slices.SortFunc(c.Examples, func(a, b *pb.ReleaseExample) int {
		return strings.Compare(a.GetExample()+"\x00"+a.GetCard().GetTask(), b.GetExample()+"\x00"+b.GetCard().GetTask())
	})
	if _, err := release.MarshalCatalogue(c); err != nil {
		return nil, err
	}
	return c, nil
}
