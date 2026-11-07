package core

import (
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// A workstream is a line of work an agent started and named. Plans join it, and what it counts and
// gained is derived by whoever reads the plans: a workstream stores only what an agent stated.

// WorkstreamStatus is where a workstream stands. Agents choose it: no rule orders the statuses.
type WorkstreamStatus string

// The workstream statuses.
const (
	WorkstreamActive WorkstreamStatus = "active"
	WorkstreamPaused WorkstreamStatus = "paused"
	WorkstreamClosed WorkstreamStatus = "closed"
)

// WorkstreamStatuses lists every workstream status, in the order a listing names them.
func WorkstreamStatuses() []WorkstreamStatus {
	return []WorkstreamStatus{WorkstreamActive, WorkstreamPaused, WorkstreamClosed}
}

// Valid says whether the status is one of the three.
func (s WorkstreamStatus) Valid() bool {
	for _, known := range WorkstreamStatuses() {
		if s == known {
			return true
		}
	}
	return false
}

// The limits on what a workstream holds, in characters. A title is a name a person says aloud, and
// a note one line of state.
const (
	MaxWorkstreamTitleRunes = 80
	MaxWorkstreamNoteRunes  = 280
)

// Workstream is a line of work, as the store keeps it.
type Workstream struct {
	// Number is the place among the task's workstreams, 1 for the first. The store assigns it and
	// never reuses it.
	Number WorkstreamNumber
	// Title is one line with its white space collapsed to single spaces and none at either end, as
	// NormalizeWorkstreamTitle writes it.
	Title  string
	Status WorkstreamStatus
	// Note is the agent's one-line state of the line, empty until it states one.
	Note string
	// Origin is who started the workstream and when.
	Origin RecordOrigin
	// UpdatedAt is when the workstream last changed: its start, or the last edit that changed it.
	UpdatedAt time.Time
}

// NewWorkstream is a request to start a workstream. The store assigns the number, and the status is
// active.
type NewWorkstream struct {
	// Title is in the form NormalizeWorkstreamTitle gives.
	Title string
	// Origin is who starts it and when, which is its start time.
	Origin RecordOrigin
}

// StartedWorkstream is what starting a workstream came to.
type StartedWorkstream struct {
	Workstream Workstream
	// Created is false when a workstream that is not closed had the title, and it is that one.
	Created bool
}

// WorkstreamEdit changes a workstream. A nil field is left as it is, and the whole edit is one
// change.
type WorkstreamEdit struct {
	// Title is in the form NormalizeWorkstreamTitle gives.
	Title  *string
	Status *WorkstreamStatus
	// Note replaces the note, and an empty one clears it.
	Note *string
	// At is when the edit is made, which is the workstream's update time if it changes anything.
	At time.Time
}

// WorkstreamFilter selects workstreams. The zero value matches every workstream.
type WorkstreamFilter struct {
	Statuses []WorkstreamStatus
}

// NormalizeWorkstreamTitle is the form a title is stored in: its runs of white space, a tab
// included, are single spaces and none is left at either end. A title that has a line break is not
// one line, and DecideWorkstreamTitle refuses it before it gets here.
func NormalizeWorkstreamTitle(title string) string {
	return strings.Join(strings.Fields(title), " ")
}

// WorkstreamTitleKey is what two titles are compared in: the normalized title with each character
// folded to the smallest of its case variants, so two titles that differ only in case or in white
// space have the same key, and two that differ in anything else have another. It is what makes
// starting a workstream of a name in use find that one.
func WorkstreamTitleKey(title string) string {
	var b strings.Builder
	for _, r := range NormalizeWorkstreamTitle(title) {
		b.WriteRune(foldRune(r))
	}
	return b.String()
}

// foldRune is the smallest rune that is r or a case variant of it, the same for every variant.
func foldRune(r rune) rune {
	least := r
	for f := unicode.SimpleFold(r); f != r; f = unicode.SimpleFold(f) {
		least = min(least, f)
	}
	return least
}

// DecideWorkstreamTitle turns what an agent wrote into the title it is stored under. It fails with
// a *Failure of kind FailureInvalidInput that names the field: a title that is blank, has a line
// break, is not valid UTF-8, or is over its limit once its white space is collapsed.
func DecideWorkstreamTitle(field, title string) (string, error) {
	switch {
	case strings.TrimSpace(title) == "":
		return "", failf(FailureInvalidInput, "%s: a workstream needs a title", field)
	case strings.ContainsAny(title, "\r\n"):
		return "", failf(FailureInvalidInput, "%s: a title is one line", field)
	case !utf8.ValidString(title):
		return "", failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	normalized := NormalizeWorkstreamTitle(title)
	if n := utf8.RuneCountInString(normalized); n > MaxWorkstreamTitleRunes {
		return "", failf(FailureInvalidInput, "%s: %d characters is over the limit of %d", field, n, MaxWorkstreamTitleRunes)
	}
	return normalized, nil
}

// CheckWorkstreamNote says whether a note is one line of valid text within its limit. It may be
// empty, which is no note.
func CheckWorkstreamNote(field, note string) error {
	switch {
	case strings.ContainsAny(note, "\r\n"):
		return failf(FailureInvalidInput, "%s: a note is one line", field)
	case !utf8.ValidString(note):
		return failf(FailureInvalidInput, "%s: not valid UTF-8", field)
	}
	if n := utf8.RuneCountInString(note); n > MaxWorkstreamNoteRunes {
		return failf(FailureInvalidInput, "%s: %d characters is over the limit of %d", field, n, MaxWorkstreamNoteRunes)
	}
	return nil
}

// Check says whether the workstream is as a store keeps it, whatever task it is in: a number, a
// title in its stored form, a status of the three, a note within its limit, and an origin and an
// update time. The error is a *Failure of kind FailureInvalidInput that names the field.
func (w Workstream) Check() error {
	if w.Number == 0 {
		return failf(FailureInvalidInput, "number: a workstream has a number from 1")
	}
	return w.checkContent()
}

func (w Workstream) checkContent() error {
	title, err := DecideWorkstreamTitle("title", w.Title)
	switch {
	case err != nil:
		return err
	case title != w.Title:
		return failf(FailureInvalidInput, "title: %q is not stored with its white space collapsed, as %q", w.Title, title)
	case !w.Status.Valid():
		return failf(FailureInvalidInput, "status: %q is not a workstream status", w.Status)
	}
	if err := w.Origin.Check("origin"); err != nil {
		return err
	}
	if w.UpdatedAt.IsZero() {
		return failf(FailureInvalidInput, "updated_at: a workstream has the time it last changed")
	}
	return CheckWorkstreamNote("note", w.Note)
}

// Check says whether the request is well formed, whatever task it is for.
func (n NewWorkstream) Check() error {
	return Workstream{Title: n.Title, Status: WorkstreamActive, Origin: n.Origin, UpdatedAt: n.Origin.At}.checkContent()
}

// Named says whether the workstream is the line of work that the title names, which is so when it
// is not closed and the keys of the two titles are the same. A closed workstream is named by no
// title, so a line of work that was closed can be started again.
func (w Workstream) Named(title string) bool {
	return w.Status != WorkstreamClosed && WorkstreamTitleKey(w.Title) == WorkstreamTitleKey(title)
}

// CheckWorkstreamTitles says whether no two of the workstreams that are not closed have the same
// title, as the store keeps it. The error is a *Failure of kind FailureConflict that names both.
func CheckWorkstreamTitles(workstreams []Workstream) error {
	seen := map[string]WorkstreamNumber{}
	for _, w := range workstreams {
		if w.Status == WorkstreamClosed {
			continue
		}
		key := WorkstreamTitleKey(w.Title)
		if other, ok := seen[key]; ok {
			return failf(FailureConflict, "workstreams %d and %d are both open under the title %q", other, w.Number, w.Title)
		}
		seen[key] = w.Number
	}
	return nil
}

// Described names a workstream for a message: its number and title.
func (w Workstream) Described() string { return fmt.Sprintf("workstream %d (%q)", w.Number, w.Title) }
