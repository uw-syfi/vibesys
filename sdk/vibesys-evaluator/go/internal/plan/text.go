package plan

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

// Text is a compiled Template: a literal, or literal parts and refs to fill from captures.
type Text struct {
	literal string
	parts   []part
}

type part struct {
	literal  string
	ref      string
	encoding pb.RefEncoding
}

// Captured is one value captured from a response.
type Captured struct {
	// Text is a JSON string's content, a header's value, or another JSON value's text.
	Text string
	// JSON is the value as JSON text (a header's value as a JSON string).
	JSON string
}

// Literal returns the text when it has no refs.
func (t Text) Literal() string { return t.literal }

// StartsWithLiteralSlash reports whether the text's first part is a literal starting with "/".
func (t Text) StartsWithLiteralSlash() bool {
	if t.parts == nil {
		return strings.HasPrefix(t.literal, "/")
	}
	return t.parts[0].ref == "" && strings.HasPrefix(t.parts[0].literal, "/")
}

// HasRefs reports whether Fill needs captures.
func (t Text) HasRefs() bool { return t.parts != nil }

// Fill returns the text with each ref replaced by its captured value in the ref's encoding. Every
// ref was checked against earlier captures when the plan was read, so a missing one is a bug.
func (t Text) Fill(captures map[string]Captured) string {
	if t.parts == nil {
		return t.literal
	}
	var out strings.Builder
	for _, p := range t.parts {
		if p.ref == "" {
			out.WriteString(p.literal)
			continue
		}
		value := captures[p.ref]
		switch p.encoding {
		case pb.RefEncoding_REF_ENCODING_JSON_STRING:
			quoted, _ := json.Marshal(value.Text)
			out.Write(quoted[1 : len(quoted)-1])
		case pb.RefEncoding_REF_ENCODING_JSON_VALUE:
			out.WriteString(value.JSON)
		default:
			out.WriteString(value.Text)
		}
	}
	return out.String()
}

func compileText(template *pb.Template, captured map[string]bool) (Text, error) {
	if template == nil {
		return Text{}, errors.New("missing template")
	}
	var parts []part
	hasRef := false
	for _, p := range template.GetParts() {
		switch kind := p.GetPart().(type) {
		case *pb.Template_Part_Literal:
			parts = append(parts, part{literal: kind.Literal})
		case *pb.Template_Part_Ref:
			name := kind.Ref.GetName()
			if !captured[name] {
				return Text{}, fmt.Errorf("ref %q names no capture of an earlier step", name)
			}
			hasRef = true
			parts = append(parts, part{ref: name, encoding: kind.Ref.GetEncoding()})
		default:
			return Text{}, errors.New("template part is neither literal nor ref")
		}
	}
	if !hasRef {
		var literal strings.Builder
		for _, p := range parts {
			literal.WriteString(p.literal)
		}
		return Text{literal: literal.String()}, nil
	}
	return Text{parts: parts}, nil
}
