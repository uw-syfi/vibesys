// Package names spells enum values the way people and agents type them, from the enum itself. The
// value AGENT_ROLE_WORKER of AgentRole is "worker" and HARNESS_CLAUDE_CODE of Harness is
// "claude_code": the enum's name in upper snake case and an underscore are dropped from the value's
// name, and what is left is lower case. A new value of an enum is spelled, listed and parsed with
// no other edit, so no table of names can drift from the enum it names.
package names

import (
	"strconv"
	"strings"
	"unicode"

	"google.golang.org/protobuf/reflect/protoreflect"
)

// Enum is a generated enum type.
type Enum interface {
	~int32
	protoreflect.Enum
}

// Spell is the spelling of one value. A number the enum does not define is spelled as its number.
func Spell[E Enum](e E) string {
	d := e.Descriptor().Values().ByNumber(e.Number())
	if d == nil {
		return strconv.Itoa(int(e.Number()))
	}
	return spelling(d)
}

// Parse reads a spelling back. The unspecified value, which every enum has at zero, is never
// spelled: an absent value is how a caller says it.
func Parse[E Enum](s string) (E, bool) {
	var zero E
	n, ok := Lookup(zero.Descriptor(), s)
	return E(n), ok
}

// Lookup is Parse for an enum known only by its descriptor.
func Lookup(d protoreflect.EnumDescriptor, s string) (protoreflect.EnumNumber, bool) {
	values := d.Values()
	for i := 0; i < values.Len(); i++ {
		if v := values.Get(i); v.Number() != 0 && spelling(v) == s {
			return v.Number(), true
		}
	}
	return 0, false
}

// Spellings lists the spelling of each value of the enum except the unspecified one, in the order
// the enum declares them. skip leaves out values by number.
func Spellings(d protoreflect.EnumDescriptor, skip ...protoreflect.EnumNumber) []string {
	var out []string
	values := d.Values()
loop:
	for i := 0; i < values.Len(); i++ {
		v := values.Get(i)
		if v.Number() == 0 {
			continue
		}
		for _, s := range skip {
			if s == v.Number() {
				continue loop
			}
		}
		out = append(out, spelling(v))
	}
	return out
}

// List is Spellings joined for a sentence: "a, b or c".
func List[E Enum](skip ...E) string {
	var zero E
	var numbers []protoreflect.EnumNumber
	for _, s := range skip {
		numbers = append(numbers, s.Number())
	}
	return Join(Spellings(zero.Descriptor(), numbers...))
}

// Join writes spellings as "a, b or c".
func Join(spellings []string) string {
	switch len(spellings) {
	case 0:
		return ""
	case 1:
		return spellings[0]
	}
	return strings.Join(spellings[:len(spellings)-1], ", ") + " or " + spellings[len(spellings)-1]
}

func spelling(v protoreflect.EnumValueDescriptor) string {
	name := string(v.Name())
	prefix := upperSnake(string(v.Parent().Name())) + "_"
	return strings.ToLower(strings.TrimPrefix(name, prefix))
}

// upperSnake turns AgentRole into AGENT_ROLE.
func upperSnake(s string) string {
	var b strings.Builder
	for i, r := range s {
		if i > 0 && unicode.IsUpper(r) {
			b.WriteByte('_')
		}
		b.WriteRune(unicode.ToUpper(r))
	}
	return b.String()
}
