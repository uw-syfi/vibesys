package task

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// Catalogue reads the [catalogue] table of a manifest into its typed definition and holds it to the
// rules of the message. A key the message has no field for is refused by name, as every other key of
// the manifest is.
func Catalogue(table map[string]any) (*cliv1.TaskCatalogueV02, error) {
	var c cliv1.TaskCatalogueV02
	if err := tomlproto.DecodeTable(table, &c, tomlproto.Options{}); err != nil {
		return nil, invalid("%s: catalogue: %s", ManifestName, core.AsFailure(err).Message)
	}
	if err := validate(ManifestName+" [catalogue]", &c); err != nil {
		return nil, err
	}
	return &c, nil
}
