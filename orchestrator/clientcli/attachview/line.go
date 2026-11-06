package attachview

import (
	"slices"
	"strconv"
	"strings"
)

// LineKind says what a line is, so that how it looks is decided where it is drawn and not where it is
// made. Update only says what a line means; Render, and no other code, gives it a marker, an indent and a
// width.
type LineKind int

// The kinds of line.
const (
	// LineMilestone is something that happened to the run or to an agent, with its time of day.
	LineMilestone LineKind = iota
	// LineTurn is the start or the end of a turn of the viewed agent, with its time of day.
	LineTurn
	// LineHeader says which agent a view is of.
	LineHeader
	// LineNotice is something the view says of itself: how much it left out, a key that meant nothing.
	LineNotice
	// LinePicker is one row of the list of agents.
	LinePicker
	// LineAssistant is the first line of what an agent said, and LineAssistantMore each line after it.
	LineAssistant
	LineAssistantMore
	// LinePrompt is what an agent was told, on one line. Label says by what: brief, steer.
	LinePrompt
	// LineTool is one tool call on one line, and LineToolFailure what went wrong with it.
	LineTool
	LineToolFailure
)

// Tone is how what a line says went, as meaning. What a tone looks like is for the renderer to say; a
// renderer with no colour may ignore it.
type Tone int

// The tones.
const (
	ToneNone Tone = iota
	// ToneGood is something that went well: a job that succeeded, a handoff, a measurement.
	ToneGood
	// ToneBad is something that failed.
	ToneBad
	// ToneWarn is something that needs a person's attention but has not failed: a limit reached.
	ToneWarn
	// ToneStrong is a line a person should not miss: a final report.
	ToneStrong
)

// Line is one line of what the view shows, as meaning. Its text is safe to print: it holds no control
// character and no line break.
//
// Subject, when it is not empty, is the words of Text that name what the line is about (an agent's
// name), and Mark the words of Text that carry Tone. A Tone with no Mark is the tone of the whole line.
// Both are parts of Text, so a renderer that ignores them prints the same words.
type Line struct {
	Kind    LineKind
	Label   string
	Text    string
	Subject string
	Mark    string
	Tone    Tone
}

func plural(n uint32, one, many string) string {
	if n == 1 {
		return one
	}
	return many
}

// DefaultWidth is where one-line forms are cut when the width is not known.
const DefaultWidth = 100

// Render is the line as it is printed in a terminal width characters wide, or DefaultWidth wide when the
// width is not known. It is the only place that decides what a line looks like.
func Render(l Line, width int) string {
	if width <= 0 {
		width = DefaultWidth
	}
	width = max(width, 8)
	switch l.Kind {
	case LineAssistant:
		return "* " + l.Text
	case LineAssistantMore:
		if strings.TrimSpace(l.Text) == "" {
			return ""
		}
		return "  " + l.Text
	case LinePrompt:
		return Clip("  ["+l.Label+"] "+l.Text, width)
	case LineTool:
		return Clip("  "+l.Text, width)
	case LineToolFailure:
		return Clip("    "+l.Text, width)
	}
	return l.Text
}

// Reset ends every span of style the renderer starts. It is also what a program writes on leaving, so
// that no style outlives the view.
const Reset = "\x1b[0m"

// look is a style: the 16 standard colours, dim and bold, so that it follows the terminal's theme. No
// background and no other colour is used.
type look struct {
	dim, bold bool
	fg        int // an SGR foreground code from 31 to 36, or zero for the terminal's own.
}

const (
	red    = 31
	green  = 32
	yellow = 33
	cyan   = 36
)

func (k look) plain() bool { return !k.dim && !k.bold && k.fg == 0 }

// wrap is text in the style, closed by Reset; text with no style, or none, is itself.
func (k look) wrap(text string) string {
	if k.plain() || text == "" {
		return text
	}
	var codes []string
	if k.bold {
		codes = append(codes, "1")
	}
	if k.dim {
		codes = append(codes, "2")
	}
	if k.fg != 0 {
		codes = append(codes, strconv.Itoa(k.fg))
	}
	return "\x1b[" + strings.Join(codes, ";") + "m" + text + Reset
}

// toneLook is how a tone looks. A good outcome is green, a failure red, something to attend to yellow,
// and what must not be missed bold.
func toneLook(t Tone) look {
	switch t {
	case ToneGood:
		return look{fg: green}
	case ToneBad:
		return look{fg: red}
	case ToneWarn:
		return look{fg: yellow}
	case ToneStrong:
		return look{bold: true}
	}
	return look{}
}

// kindLook is how a kind of line looks before its subject and tone. What an agent said is the
// terminal's own style, so it stays the plainest; what is about the work is dim.
func kindLook(k LineKind) look {
	switch k {
	case LineMilestone, LineTurn, LineHeader, LineNotice, LineTool:
		return look{dim: true}
	case LineToolFailure:
		return look{fg: red}
	case LinePrompt:
		return look{fg: cyan}
	}
	return look{}
}

// RenderColor is Render with style. It cuts the line to the width first, exactly as Render does, and
// only then marks spans of what is left, so colour never changes where a line is cut or what it says:
// with the sequences it adds removed, the result is Render's. It writes only sequences of its own, each
// span is closed by Reset, a line that used style ends with one, and a line that starts no span has none.
func RenderColor(l Line, width int) string {
	plain := Render(l, width)
	if strings.ContainsRune(plain, 0x1b) {
		plain = Sanitize(plain) // a line is sanitised before it gets here; this is for one that was not.
	}
	if plain == "" {
		return ""
	}
	base := kindLook(l.Kind)
	if l.Mark == "" && l.Tone != ToneNone {
		base = toneLook(l.Tone)
	}
	type span struct {
		from, to int
		style    look
	}
	var spans []span
	end := 0
	if l.Subject != "" {
		if i := strings.Index(plain, l.Subject); i >= 0 {
			style := look{bold: true} // the name stands out from the dim line around it.
			spans = append(spans, span{i, i + len(l.Subject), style})
			end = i + len(l.Subject)
		}
	}
	if l.Mark != "" && l.Tone != ToneNone {
		if i := strings.Index(plain[end:], l.Mark); i >= 0 {
			spans = append(spans, span{end + i, end + i + len(l.Mark), toneLook(l.Tone)})
		}
	}
	slices.SortFunc(spans, func(a, b span) int { return a.from - b.from })
	var b strings.Builder
	at := 0
	for _, s := range spans {
		b.WriteString(base.wrap(plain[at:s.from]))
		b.WriteString(s.style.wrap(plain[s.from:s.to]))
		at = s.to
	}
	b.WriteString(base.wrap(plain[at:]))
	out := b.String()
	if out != plain && !strings.HasSuffix(out, Reset) {
		out += Reset // a line that used style ends reset, whatever it ends with.
	}
	return out
}

// StyleFooter is the footer line as it is drawn with colour: dim. The footer is cut to the width
// before this, and a style has no width.
func StyleFooter(text string) string {
	return look{dim: true}.wrap(Sanitize(text))
}
