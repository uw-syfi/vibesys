package archcheck

import (
	"fmt"
	"go/ast"
	"slices"
	"strings"
)

// Every function that decodes what a peer sent is fuzzed, because the peer is not trusted to send
// only what the encoders produce: an exported To* function in internal/wire must be named in
// wire's fuzz tests, or be listed here with the reason a fuzz target already covers it.

const (
	wireAt     = "cli/internal/wire/"
	wireFuzzAt = "cli/internal/wire/fuzz_test.go"
)

// unfuzzedDecoders lists the wire decoders that have no fuzz target of their own.
var unfuzzedDecoders = map[string]string{
	"ToFingerprint":     "decoded inside ToJob, which FuzzDecodeJob fuzzes",
	"ToNote":            "decoded inside ToJob, which FuzzDecodeJob fuzzes",
	"ToCallContext":     "copies five string fields and refuses nothing; a copy has no input to fuzz",
	"ToVersions":        "copies three integers and refuses nothing; a copy has no input to fuzz",
	"ToFailure":         "decoded inside FailureOf, which FuzzFailureFromStatus fuzzes",
	"ToJobState":        "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
	"ToJobKind":         "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
	"ToEnvironmentKind": "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
	"ToBackendKind":     "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
	"ToExecKind":        "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
	"ToCompatibility":   "an enum translator, held total over every contract value by TestEveryContractEnumValueIsTranslatedOrRefused",
}

// UnfuzzedDecoders returns the decoders excused from having a fuzz target, with the reason.
func UnfuzzedDecoders() map[string]string {
	out := make(map[string]string, len(unfuzzedDecoders))
	for name, reason := range unfuzzedDecoders {
		out[name] = reason
	}
	return out
}

// decoderViolations checks the wire package's decoders against its fuzz tests. It runs once per
// call to Violations, over the files given.
func decoderViolations(files map[string]string) []string {
	fuzz, haveFuzz := files[wireFuzzAt]
	var found []string
	seen := map[string]bool{}
	for name, source := range files {
		if !strings.HasPrefix(name, wireAt) || strings.Count(strings.TrimPrefix(name, wireAt), "/") > 0 ||
			!strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		file, err := parseFull(name, source)
		if err != nil {
			continue
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Recv != nil || !fn.Name.IsExported() || !strings.HasPrefix(fn.Name.Name, "To") {
				continue
			}
			decoder := fn.Name.Name
			seen[decoder] = true
			_, excused := unfuzzedDecoders[decoder]
			switch {
			case excused:
			case !haveFuzz || !strings.Contains(fuzz, "wire."+decoder+"("):
				found = append(found, fmt.Sprintf("%s: decoder %s is not called by a fuzz target in internal/wire/fuzz_test.go; fuzz it or list it in unfuzzedDecoders with the reason", name, decoder))
			}
		}
	}
	// A listed decoder that no longer exists is a stale exception, once wire is among the files.
	if len(seen) > 0 {
		for _, decoder := range sortedKeys(unfuzzedDecoders) {
			if !seen[decoder] {
				found = append(found, fmt.Sprintf("%s: unfuzzedDecoders lists %s, which wire no longer defines; remove it", wireAt, decoder))
			}
		}
	}
	return found
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}
