// Package filestore will implement store.Store on disk.
//
// It is the only code in the module that knows the physical layout: where state lives, the file
// names, the encoding of each record and the per-user index. It owns migrations, and it keeps
// that encoding separate from the domain types in internal/core and the generated contract types
// in gen/. The layout rule in internal/archcheck keeps every other package from naming it.
package filestore
