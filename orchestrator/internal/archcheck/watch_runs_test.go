package archcheck_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/archcheck"
)

func TestWatchRunsReadersUseDecoder(t *testing.T) {
	for _, expression := range []string{"m.GetChange()", "m.GetSnapshot()", "m.GetSearch()", "m.GetBody()", "m.Body", "w.Change", "m.Body.(*WatchRunsResponse_Change)", "m.Body.(*WatchRunsResponse_Snapshot)", "m.Body.(*WatchRunsResponse_Search)", "func() any {switch m.Body.(type) {case *WatchRunsResponse_Change:}; return nil}()", "m.GetChange", "(*WatchRunsResponse).GetChange", "(*Alias)(m).GetChange()", "other.GetChange()", "embedded.GetChange()", "embedded.Body", "(*Rebased)(m).Body"} {
		t.Run(expression, func(t *testing.T) {
			fs := token.NewFileSet()
			file, err := parser.ParseFile(fs, "reader.go", `package pb
 type isWatchRunsResponse_Body interface{}
 type WatchRunsResponse struct { Body isWatchRunsResponse_Body }
 type Rebased WatchRunsResponse
 type Embedded struct {*WatchRunsResponse}
 type Alias = WatchRunsResponse
 type FollowResponse struct{}
 func (*FollowResponse) GetChange() any {return nil}
 type WatchRunsResponse_Change struct { Change any }
 type WatchRunsResponse_Snapshot struct { Snapshot any }
 type WatchRunsResponse_Search struct { Search any }
 func (*WatchRunsResponse) GetChange() any {return nil}
 func (*WatchRunsResponse) GetSnapshot() any {return nil}
 func (*WatchRunsResponse) GetSearch() any {return nil}
 func (*WatchRunsResponse) GetBody() any {return nil}
 func read(m *WatchRunsResponse, w *WatchRunsResponse_Change, other *FollowResponse, embedded *Embedded) { _ = `+expression+` }
 `, 0)
			if err != nil {
				t.Fatal(err)
			}
			info := &types.Info{Types: map[ast.Expr]types.TypeAndValue{}, Selections: map[*ast.SelectorExpr]*types.Selection{}}
			config := types.Config{}
			_, err = config.Check(module+"gen/vibesys/orchestrator/v1", fs, []*ast.File{file}, info)
			if err != nil {
				t.Fatal(err)
			}
			for _, name := range []string{"orchestrator/internal/client/reader_test.go", "gateway/e2e/reader_test.go"} {
				got := archcheck.WatchRunsReaders(name, file, fs, info)
				if (len(got) > 0) != (expression != "other.GetChange()") {
					t.Fatalf("%s: %v", expression, got)
				}
			}
			if got := archcheck.WatchRunsReaders("orchestrator/internal/client/watch_runs.go", file, fs, info); len(got) != 0 {
				t.Fatal(got)
			}
		})
	}
}
