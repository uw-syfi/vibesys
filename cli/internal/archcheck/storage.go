package archcheck

import (
	"fmt"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
)

// Whatever a store persists is a message of proto/vibesys/cli/v1/store.proto in protojson, so the
// stored form is generated from the contract and not described a second time by hand. The packages
// under internal/store may therefore import no encoding that describes a record by hand: a struct
// with json tags, a gob stream, an XML or TOML or YAML document. Their writers take a proto.Message,
// so the type system holds the other half: a hand-kept struct has nothing to be passed to.

const storeAt = "cli/internal/store/"

// handEncodings are the imports the packages under internal/store may not make, with what to use.
var handEncodings = map[string]string{
	"encoding/json":              "protojson of a message in store.proto",
	"encoding/gob":               "protojson of a message in store.proto",
	"encoding/xml":               "protojson of a message in store.proto",
	"encoding/csv":               "protojson of a message in store.proto",
	"github.com/BurntSushi/toml": "protojson of a message in store.proto",
}

// storageEncoding reports each import of a hand encoding in a file of a package under
// internal/store, tests included, since a test that reads a record by hand pins a second format.
func storageEncoding(name, source string) []string {
	if !strings.HasPrefix(name, storeAt) {
		return nil
	}
	file, err := parser.ParseFile(token.NewFileSet(), name, source, parser.ImportsOnly)
	if err != nil {
		return nil // layering reports a file it cannot parse
	}
	var found []string
	for _, spec := range file.Imports {
		target, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			continue
		}
		if use, bad := handEncodings[target]; bad {
			found = append(found, fmt.Sprintf(
				"%s: imports %s; a store persists %s, so that the stored form is generated from the contract and not kept by hand", name, target, use))
		}
	}
	return found
}
