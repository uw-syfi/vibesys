package evaluator

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"unicode/utf8"
)

// Protocol is the result protocol version the records are written in.
const Protocol = 2

// maxMessage is the longest error message, in characters; a longer one is cut.
const maxMessage = 4000

// RecordError is a record the protocol does not allow; the message names the metric or rule.
type RecordError struct{ Msg string }

func (e *RecordError) Error() string { return e.Msg }

func recordErrorf(format string, args ...any) error {
	return &RecordError{Msg: fmt.Sprintf(format, args...)}
}

// Partial is what a failed sample measured before it stopped (an error record's partial).
type Partial struct {
	Name      string
	Value     float64
	Direction Direction
	Unit      string
}

// Outcome is how a record stream ended.
type Outcome string

// The outcomes of a stream: none yet, a result, or an error.
const (
	NoOutcome     Outcome = ""
	OutcomeResult Outcome = "result"
	OutcomeError  Outcome = "error"
)

// Stream is the record stream of one sample (result protocol 2), written as records are made:
// hello first, then at most one outcome. Each line is flushed to disk before the call returns.
// It refuses any record a reader would reject, so a task's mistake surfaces here with its name.
// A Stream is not safe for concurrent use.
type Stream struct {
	path    string
	file    *os.File
	metrics []string // declared names, in order; nil before hello
	outcome Outcome
	hello   bool
}

// NewStream is the stream that will be written to path; the file is created (and truncated)
// with its first record.
func NewStream(path string) *Stream { return &Stream{path: path} }

// OpenStream is the stream already written to path, to which more records may be appended: its
// declared metrics and outcome are read back from the file.
func OpenStream(path string) (*Stream, error) {
	s := &Stream{path: path}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return s, nil
	}
	if err != nil {
		return nil, err
	}
	sc := bufio.NewScanner(bytes.NewReader(data))
	sc.Buffer(nil, 1<<24)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var rec struct {
			Kind    string                     `json:"kind"`
			Metrics map[string]json.RawMessage `json:"metrics"`
		}
		if err := json.Unmarshal(line, &rec); err != nil {
			return nil, fmt.Errorf("%s is not a record stream: %w", path, err)
		}
		switch rec.Kind {
		case "hello":
			s.hello = true
			// The declared order is the line's own; json maps lose it, so read it from the text.
			s.metrics = declaredNames(line)
		case "result":
			s.outcome = OutcomeResult
		case "error":
			s.outcome = OutcomeError
		}
	}
	return s, sc.Err()
}

// declaredNames reads the metric names of a hello line in the order they appear.
func declaredNames(line []byte) []string {
	var rec struct {
		Metrics json.RawMessage `json:"metrics"`
	}
	if json.Unmarshal(line, &rec) != nil {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(rec.Metrics))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return nil
	}
	var names []string
	for dec.More() {
		key, err := dec.Token()
		if err != nil {
			break
		}
		name, _ := key.(string)
		names = append(names, name)
		var skip json.RawMessage
		if dec.Decode(&skip) != nil {
			break
		}
	}
	return names
}

// Outcome is the outcome written so far.
func (s *Stream) Outcome() Outcome { return s.outcome }

// Hello declares the metrics, in order (the first record).
func (s *Stream) Hello(metrics []NamedMetric) error {
	if s.hello || s.outcome != NoOutcome {
		return recordErrorf("hello is written once, first")
	}
	if len(metrics) == 0 {
		return recordErrorf("declare at least one metric")
	}
	var b strings.Builder
	b.WriteString(`{"kind":"hello","protocol":2,"metrics":{`)
	seen := map[string]bool{}
	names := make([]string, 0, len(metrics))
	for i, m := range metrics {
		if !ValidMetricName(m.Name) {
			return recordErrorf("metric name %s must be non-empty and contain no whitespace", pyRepr(m.Name))
		}
		if err := m.Validate(); err != nil {
			return &RecordError{Msg: err.Error()}
		}
		if seen[m.Name] {
			return recordErrorf("metric %s is declared twice", pyRepr(m.Name))
		}
		seen[m.Name] = true
		names = append(names, m.Name)
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(jsonString(m.Name) + `:{"direction":` + jsonString(string(m.Direction)))
		if m.Unit != "" {
			b.WriteString(`,"unit":` + jsonString(m.Unit))
		}
		b.WriteByte('}')
	}
	b.WriteString("}}")
	if err := s.write(b.String()); err != nil {
		return err
	}
	s.hello = true
	s.metrics = names
	return nil
}

// Result writes the measured row: every declared metric, finite. Values come out in the
// declared order.
func (s *Stream) Result(values map[string]float64) error {
	if !s.hello {
		return recordErrorf("write hello before the result")
	}
	if s.outcome != NoOutcome {
		return recordErrorf("the stream already has its outcome (%s); report once", s.outcome)
	}
	var unknown, missing []string
	for name := range values {
		if !slices.Contains(s.metrics, name) {
			unknown = append(unknown, name)
		}
	}
	for _, name := range s.metrics {
		if _, ok := values[name]; !ok {
			missing = append(missing, name)
		}
	}
	if len(unknown) > 0 {
		slices.Sort(unknown)
		declared := slices.Clone(s.metrics)
		slices.Sort(declared)
		return recordErrorf("reported %s but declared only %s", pyList(unknown), pyList(declared))
	}
	if len(missing) > 0 {
		slices.Sort(missing)
		return recordErrorf("declared %s but did not report them", pyList(missing))
	}
	for _, name := range s.metrics {
		if v := values[name]; !Finite(v) {
			return recordErrorf("metric %s is %s; report a finite number", name, pyFloat(v))
		}
	}
	var b strings.Builder
	b.WriteString(`{"kind":"result","label":"","values":{`)
	for i, name := range s.metrics {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(jsonString(name) + ":" + pyFloat(values[name]))
	}
	b.WriteString("}}")
	if err := s.write(b.String()); err != nil {
		return err
	}
	s.outcome = OutcomeResult
	return nil
}

// Error writes an error record; it ends the stream (a result before it is ignored by readers).
// A message is trimmed, and cut to 4000 characters. The partial is kept only when it is
// something a reader accepts.
func (s *Stream) Error(message string, partial *Partial) error {
	if s.outcome == OutcomeError {
		return recordErrorf("the stream already ended with an error")
	}
	text := strings.TrimSpace(message)
	if text == "" {
		text = "the benchmark failed without a message"
	}
	if utf8.RuneCountInString(text) > maxMessage {
		text = string([]rune(text)[:maxMessage-3]) + "..."
	}
	rec := `{"kind":"error","message":` + jsonString(text)
	if partial != nil && ValidMetricName(partial.Name) && Finite(partial.Value) {
		rec += `,"partial":{"name":` + jsonString(partial.Name) + `,"value":` + pyFloat(partial.Value) +
			`,"direction":` + jsonString(string(partial.Direction))
		if partial.Unit != "" {
			rec += `,"unit":` + jsonString(partial.Unit)
		}
		rec += "}"
	}
	if err := s.write(rec + "}"); err != nil {
		return err
	}
	s.outcome = OutcomeError
	return nil
}

func (s *Stream) write(line string) error {
	if s.file == nil {
		flags := os.O_WRONLY | os.O_CREATE | os.O_APPEND
		if !s.hello && s.outcome == NoOutcome {
			flags |= os.O_TRUNC
		}
		if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
			return err
		}
		f, err := os.OpenFile(s.path, flags, 0o644)
		if err != nil {
			return err
		}
		s.file = f
	}
	if _, err := s.file.WriteString(line + "\n"); err != nil {
		return err
	}
	return s.file.Sync()
}

// Close closes the file (idempotent).
func (s *Stream) Close() error {
	if s.file == nil {
		return nil
	}
	err := s.file.Close()
	s.file = nil
	return err
}
