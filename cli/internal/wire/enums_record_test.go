package wire_test

import (
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// checkEnum tries the translators of one stored enumeration on every value the contract names,
// which it reads from the contract's own table of them: each goes to a domain value and comes back
// as itself, only the unspecified value is the domain's zero value, and a number no build names is
// refused and never read as a value.
func checkEnum[W ~int32, D ~string](t *testing.T, what string, values map[int32]string, to func(string, W) (D, error), from func(D) W) {
	t.Helper()
	for n, name := range values {
		domain, err := to(what, W(n))
		if err != nil || from(domain) != W(n) {
			t.Errorf("%s: %s went to %q, %v and came back as %d", what, name, domain, err, from(domain))
		}
		if (n == 0) != (domain == "") {
			t.Errorf("%s: %s is %q, and only the unspecified value is the zero value", what, name, domain)
		}
	}
	if got, err := to(what, W(-12345)); err == nil {
		t.Errorf("%s: a value no build names was read as %q", what, got)
	}
}

func TestEveryRecordEnumValueIsTranslatedOrRefused(t *testing.T) {
	checkEnum(t, "verdict kind", cliv1.VerdictKind_name, wire.ToVerdictKind, wire.VerdictKind)
	checkEnum(t, "reason", cliv1.InconclusiveReason_name, wire.ToInconclusiveReason, wire.InconclusiveReason)
	checkEnum(t, "refusal kind", cliv1.PromoteRefusalKind_name, wire.ToPromoteRefusalKind, wire.PromoteRefusalKind)
	checkEnum(t, "direction", cliv1.Direction_name, wire.ToDirection, wire.Direction)
	checkEnum(t, "command", cliv1.Command_name, wire.ToCommand, wire.Command)
	checkEnum(t, "outcome", cliv1.CommandOutcome_name, wire.ToCommandOutcome, wire.CommandOutcome)
}
