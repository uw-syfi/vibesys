package attachview

import "strings"

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

// Line is one line of what the view shows, as meaning. Its text is safe to print: it holds no control
// character and no line break.
type Line struct {
	Kind  LineKind
	Label string
	Text  string
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
