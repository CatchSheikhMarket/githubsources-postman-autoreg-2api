// nativetools.go — трансляция нативных вызовов агента Postman в инструменты,
// объявленные клиентом.
//
// Даже когда запрос несёт tools (product workspace_tools_sdk_localmode),
// апстрим может выдать вызов своего нативного инструмента — listDirectory,
// readFile, executeBashCommand… Такие вызовы переводятся в эквиваленты из
// клиентского набора (Bash/Read/Write/Grep), клиент выполняет их сам, а
// результат уходит апстриму под тем же toolCallId. Клиент никогда не видит
// имён, которых не объявлял (иначе его harness падает с «Tool not found»).
// Перевод — по образцу живых мостов Postman (listDirectory → Bash(find) и
// т.п.).
package app

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// clientToolSet — набор инструментов, объявленных клиентом.
type clientToolSet struct {
	names []string
	has   map[string]bool
}

func newClientToolSet(tools []ToolDef) *clientToolSet {
	c := &clientToolSet{has: make(map[string]bool, len(tools))}
	for _, t := range tools {
		if t.Name == "" || c.has[t.Name] {
			continue
		}
		c.has[t.Name] = true
		c.names = append(c.names, t.Name)
	}
	sort.Strings(c.names)
	return c
}

func (c *clientToolSet) hasAny(names ...string) bool {
	for _, n := range names {
		if c.has[n] {
			return true
		}
	}
	return false
}

// nativeToolNeeds — какие клиентские инструменты закрывают нативный вызов
// апстрима. ok=false — имя не из известного набора нативных инструментов.
func nativeToolNeeds(native string) ([]string, bool) {
	switch native {
	case "executeBashCommand", "listDirectory", "listNamespaces", "listNamespaceTools":
		return []string{"Bash"}, true
	case "readFile":
		return []string{"Read"}, true
	case "createFile", "writeToFile":
		return []string{"Write"}, true
	case "searchInFiles":
		return []string{"Grep", "Bash"}, true
	}
	return nil, false
}

// translateNativeCall переводит нативный вызов в клиентский инструмент.
// Вызывается только когда nativeToolNeeds удовлетворён; сборка best-effort:
// недостающие поля остаются пустыми, клиент вернёт моделью понятную ошибку.
func translateNativeCall(native string, in map[string]any, c *clientToolSet) (string, map[string]any) {
	switch native {
	case "executeBashCommand":
		return "Bash", compact(map[string]any{
			"command":           argString(in, "command"),
			"description":       argString(in, "description"),
			"timeout":           in["timeout"],
			"run_in_background": argBool(in, "isBackground", "run_in_background"),
		})
	case "readFile":
		return "Read", compact(map[string]any{
			"file_path": argString(in, "file_path", "filePath", "path"),
			"offset":    in["offset"],
			"limit":     in["limit"],
			"pages":     in["pages"],
		})
	case "createFile", "writeToFile":
		return "Write", compact(map[string]any{
			"file_path": argString(in, "file_path", "filePath", "path"),
			"content":   argString(in, "content", "contents"),
		})
	case "listDirectory":
		return "Bash", map[string]any{
			"command":     findCommand(in),
			"description": "list directory contents",
		}
	case "searchInFiles":
		if c.has["Grep"] {
			return "Grep", grepArgs(in)
		}
		return "Bash", map[string]any{
			"command":     rgCommand(in, c),
			"description": "search in files",
		}
	case "listNamespaces":
		return "Bash", map[string]any{
			"command":     "printf '%s\\n' 'external-client'",
			"description": "list tool namespaces",
		}
	case "listNamespaceTools":
		quoted := make([]string, 0, len(c.names))
		for _, n := range c.names {
			quoted = append(quoted, shellQuote(n))
		}
		return "Bash", map[string]any{
			"command":     "printf '%s\\n' " + strings.Join(quoted, " "),
			"description": "list client tools",
		}
	}
	return native, in
}

// findCommand — listDirectory → find с глубиной и игнор-глобами.
func findCommand(in map[string]any) string {
	dir := argString(in, "directoryPath", "dir", "path", "directory")
	if dir == "" {
		dir = "."
	}
	depth := 1
	if d, ok := argInt(in, "depth"); ok {
		if d < 1 {
			d = 1
		}
		if d > 20 {
			d = 20
		}
		depth = d
	}
	var b strings.Builder
	fmt.Fprintf(&b, "find %s -mindepth 1 -maxdepth %d", shellQuote(dir), depth)
	if globs, ok := in["ignoreGlobs"].([]any); ok {
		for _, g := range globs {
			s, ok := g.(string)
			if !ok || strings.TrimSpace(s) == "" {
				continue
			}
			fmt.Fprintf(&b, " ! -path %s", shellQuote(dir+"/"+s))
			fmt.Fprintf(&b, " ! -path %s", shellQuote(dir+"/"+s+"/*"))
		}
	}
	b.WriteString(" -print | sort")
	return b.String()
}

func grepArgs(in map[string]any) map[string]any {
	query := argString(in, "query")
	pattern := query
	if b, ok := argBoolIn(in, "isRegex"); !ok || !b {
		pattern = escapeRegex(query)
	}
	gpath, globs := searchScopeLite(in)
	ignoreCase := false
	if b, ok := argBoolIn(in, "caseSensitive", "isCaseSensitive"); ok && !b {
		ignoreCase = true
	}
	return compact(map[string]any{
		"pattern":     pattern,
		"path":        gpath,
		"glob":        combinedGlob(globs),
		"output_mode": "content",
		"-n":          true,
		"-i":          ignoreCase,
		"head_limit":  searchLimit(in),
	})
}

func rgCommand(in map[string]any, _ *clientToolSet) string {
	parts := []string{"rg", "--line-number", "--no-heading", "--color", "never"}
	if b, ok := argBoolIn(in, "isRegex"); !ok || !b {
		parts = append(parts, "--fixed-strings")
	}
	if b, ok := argBoolIn(in, "caseSensitive", "isCaseSensitive"); ok && !b {
		parts = append(parts, "--ignore-case")
	}
	gpath, globs := searchScopeLite(in)
	if gpath == "" {
		gpath = "."
	}
	for _, g := range globs {
		parts = append(parts, "--glob", shellQuote(g))
	}
	parts = append(parts, "--", shellQuote(argString(in, "query")), shellQuote(gpath))
	return strings.Join(parts, " ") + " | sed -n " + shellQuote(fmt.Sprintf("1,%dp", searchLimit(in)))
}

// searchScopeLite — область поиска searchInFiles: явный каталог и файловые
// шаблоны. Ветку «все шаблоны абсолютные → общий корень» референсного моста
// не портируем: агент апстрима в наших запросах присылает относительные пути.
func searchScopeLite(in map[string]any) (string, []string) {
	var patterns []string
	if raw, ok := in["fileNamePatterns"].([]any); ok {
		for _, p := range raw {
			if s, ok := p.(string); ok && strings.TrimSpace(s) != "" {
				patterns = append(patterns, strings.TrimSpace(s))
			}
		}
	}
	if len(patterns) == 0 {
		if s := argString(in, "fileNamePattern"); s != "" {
			patterns = []string{s}
		}
	}
	return argString(in, "directoryPath", "rootPath", "path"), patterns
}

func combinedGlob(globs []string) string {
	switch len(globs) {
	case 0:
		return ""
	case 1:
		return globs[0]
	}
	return "{" + strings.Join(globs, ",") + "}"
}

func searchLimit(in map[string]any) int {
	limit, ok := argInt(in, "maxResults")
	if !ok {
		limit, ok = argInt(in, "limit")
	}
	if !ok {
		limit = 200
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 1000 {
		limit = 1000
	}
	return limit
}

// ── Хелперы ──

func parsedArgs(s string) map[string]any {
	m := map[string]any{}
	if strings.TrimSpace(s) != "" {
		_ = json.Unmarshal([]byte(s), &m)
	}
	return m
}

func argString(in map[string]any, keys ...string) string {
	for _, k := range keys {
		if v, ok := in[k]; ok {
			if s, is := v.(string); is && s != "" {
				return s
			}
		}
	}
	return ""
}

func argInt(in map[string]any, keys ...string) (int, bool) {
	for _, k := range keys {
		v, exists := in[k]
		if !exists {
			continue
		}
		switch n := v.(type) {
		case float64:
			return int(n), true
		case string:
			if i, err := strconv.Atoi(strings.TrimSpace(n)); err == nil {
				return i, true
			}
		}
	}
	return 0, false
}

func argBool(in map[string]any, keys ...string) bool {
	b, _ := argBoolIn(in, keys...)
	return b
}

func argBoolIn(in map[string]any, keys ...string) (bool, bool) {
	for _, k := range keys {
		if v, ok := in[k]; ok {
			if b, is := v.(bool); is {
				return b, true
			}
		}
	}
	return false, false
}

// compact выкидывает nil и пустые строки.
func compact(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		if v == nil {
			continue
		}
		if s, ok := v.(string); ok && s == "" {
			continue
		}
		if b, ok := v.(bool); ok && !b {
			continue
		}
		out[k] = v
	}
	return out
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'"
}

var regexEsc = regexp.MustCompile(`[.*+?^${}()|[\]\\]`)

func escapeRegex(s string) string {
	return regexEsc.ReplaceAllStringFunc(s, func(m string) string { return `\` + m })
}
