package mcp

import (
	"embed"
	"strings"
	"text/template"

	"github.com/uw-syfi/vibesys/orchestrator/internal/names"
)

//go:embed templates/*.tmpl
var answerFiles embed.FS

// answers holds the template of each answer the tool server gives, by name. A missing field of the
// data is an error, never an empty word.
var answers = template.Must(template.New("answers").Option("missingkey=error").
	Funcs(template.FuncMap{"list": names.Join}).ParseFS(answerFiles, "templates/*.tmpl"))

// say words one answer from its data. The data of every template is built in code that a test
// runs, so a template that cannot be filled is a defect found there and not by an agent.
func say(name string, data any) string {
	var out strings.Builder
	if err := answers.ExecuteTemplate(&out, name, data); err != nil {
		return "the tool server could not word its answer (" + name + "): tell the operator"
	}
	return out.String()
}
