// Print the public hashes for the cross-language conformance check.
package main

import (
	"fmt"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
)

func main() {
	fmt.Println(schemahash.APISchemaHash)
	fmt.Println(schemahash.StoreSchemaHash)
	fmt.Println(schemahash.HostSchemaHash)
}
