package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path"
	"slices"
	"strconv"
	"strings"
)

// A switch over a closed set either names every member or says with a default what the rest do, so
// a member added later cannot be passed over in silence. The rule reads the source and has no type
// information, so it goes by what a case names: a switch with a case naming a member of a set is a
// switch over that set. The sets are found and not listed where they can be: every interface closed
// by an unexported marker method is one. A switch that names no member is not held to anything.

// closedEnums are the enums the rule holds, as package directory and type name.
var closedEnums = []string{
	"cli/internal/core.ExecKind",
	"cli/internal/core.JobState",
	"cli/internal/lifecycle.Cause",
	"cli/internal/lifecycle.DeathProof",
	"cli/internal/lifecycle.Phase",
	"cli/internal/lifecycle.RunPhase",
}

// closedSet is the members of one set, each as package directory and name.
type closedSet struct {
	name    string
	members []string
}

// closedSets returns every set found in the non-test Go files.
func closedSets(files map[string]string) []closedSet {
	union := map[string][]string{}
	enum := map[string][]string{}
	for name, source := range files {
		if !isProduct(name) {
			continue
		}
		file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
		if err != nil {
			continue
		}
		dir := path.Dir(name)
		for _, decl := range file.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if t, ok := markerOf(d); ok {
					key := dir + "." + d.Name.Name
					union[key] = append(union[key], dir+"."+t)
				}
			case *ast.GenDecl:
				for key, member := range enumMembers(dir, d) {
					enum[key] = append(enum[key], member...)
				}
			}
		}
	}
	var out []closedSet
	for key, members := range union {
		out = append(out, closedSet{key, members})
	}
	for _, key := range closedEnums {
		out = append(out, closedSet{key, enum[key]})
	}
	slices.SortFunc(out, func(a, b closedSet) int { return strings.Compare(a.name, b.name) })
	return out
}

func isProduct(name string) bool {
	return strings.HasSuffix(name, ".go") && strings.HasPrefix(name, "cli/") &&
		!strings.HasSuffix(name, "_test.go") && !strings.HasPrefix(name, genAt)
}

// markerOf returns the type a closing marker method is on: unexported, no parameters, no results,
// an empty body.
func markerOf(d *ast.FuncDecl) (string, bool) {
	if d.Recv == nil || len(d.Recv.List) != 1 || d.Name.IsExported() || d.Body == nil || len(d.Body.List) != 0 {
		return "", false
	}
	if d.Type.Params.NumFields() != 0 || d.Type.Results.NumFields() != 0 {
		return "", false
	}
	t := d.Recv.List[0].Type
	if star, ok := t.(*ast.StarExpr); ok {
		t = star.X
	}
	id, ok := t.(*ast.Ident)
	if !ok {
		return "", false
	}
	return id.Name, true
}

// enumMembers returns the constants a const declaration gives each type.
func enumMembers(dir string, d *ast.GenDecl) map[string][]string {
	out := map[string][]string{}
	if d.Tok != token.CONST {
		return out
	}
	current := ""
	for _, spec := range d.Specs {
		v, ok := spec.(*ast.ValueSpec)
		if !ok {
			continue
		}
		switch {
		case v.Type != nil:
			current = ""
			if id, ok := v.Type.(*ast.Ident); ok {
				current = id.Name
			}
		case len(v.Values) != 0:
			current = ""
		}
		if current == "" {
			continue
		}
		for _, name := range v.Names {
			out[dir+"."+current] = append(out[dir+"."+current], dir+"."+name.Name)
		}
	}
	return out
}

// switchViolations checks every switch in the non-test Go files against the closed sets.
func switchViolations(files map[string]string) []string {
	sets := closedSets(files)
	var found []string
	for name, source := range files {
		if !isProduct(name) {
			continue
		}
		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, name, source, 0)
		if err != nil {
			continue
		}
		imports := importsOf(file)
		dir := path.Dir(name)
		ast.Inspect(file, func(n ast.Node) bool {
			var body *ast.BlockStmt
			switch s := n.(type) {
			case *ast.SwitchStmt:
				if s.Tag == nil {
					return true
				}
				body = s.Body
			case *ast.TypeSwitchStmt:
				body = s.Body
			default:
				return true
			}
			named, hasDefault := casesOf(body, dir, imports)
			if hasDefault {
				return true
			}
			for _, set := range sets {
				touched := namedIn(set, named)
				if len(touched) == 0 || explainedByWhole(set, touched, sets, named) {
					continue
				}
				var missing []string
				for _, m := range set.members {
					if !slices.Contains(named, m) {
						missing = append(missing, path.Base(m))
					}
				}
				if len(missing) != 0 {
					slices.Sort(missing)
					found = append(found, fmt.Sprintf("%s:%d: a switch over %s does not name %s; name them or add a default that says what the rest do",
						name, fset.Position(n.Pos()).Line, path.Base(set.name), strings.Join(missing, ", ")))
				}
			}
			return true
		})
	}
	return found
}

// namedIn returns the members of the set that a switch names.
func namedIn(set closedSet, named []string) []string {
	var out []string
	for _, m := range set.members {
		if slices.Contains(named, m) {
			out = append(out, m)
		}
	}
	return out
}

// explainedByWhole says whether the members a switch names in a set are all members of another set
// the switch names whole. A type can belong to two sets, such as a refusal that is both a
// promotion decision and an event detail, and a switch over the one set then touches the other
// without being a switch over it.
func explainedByWhole(set closedSet, touched []string, sets []closedSet, named []string) bool {
	for _, other := range sets {
		if other.name == set.name || len(namedIn(other, named)) != len(other.members) {
			continue
		}
		if !slices.ContainsFunc(touched, func(m string) bool { return !slices.Contains(other.members, m) }) {
			return true
		}
	}
	return false
}

func importsOf(file *ast.File) map[string]string {
	out := map[string]string{}
	for _, spec := range file.Imports {
		target, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			continue
		}
		short := path.Base(target)
		if spec.Name != nil {
			short = spec.Name.Name
		}
		if rest, ok := strings.CutPrefix(target, modulePath); ok {
			out[short] = "cli/" + rest
		}
	}
	return out
}

// casesOf returns the members a switch's cases name, and whether it has a default.
func casesOf(body *ast.BlockStmt, dir string, imports map[string]string) ([]string, bool) {
	var named []string
	for _, stmt := range body.List {
		clause, ok := stmt.(*ast.CaseClause)
		if !ok {
			continue
		}
		if clause.List == nil {
			return nil, true
		}
		for _, expr := range clause.List {
			if m, ok := memberOf(expr, dir, imports); ok {
				named = append(named, m)
			}
		}
	}
	return named, false
}

func memberOf(expr ast.Expr, dir string, imports map[string]string) (string, bool) {
	if star, ok := expr.(*ast.StarExpr); ok {
		expr = star.X
	}
	switch e := expr.(type) {
	case *ast.Ident:
		return dir + "." + e.Name, true
	case *ast.SelectorExpr:
		if id, ok := e.X.(*ast.Ident); ok {
			if target, ok := imports[id.Name]; ok {
				return target + "." + e.Sel.Name, true
			}
		}
	}
	return "", false
}
