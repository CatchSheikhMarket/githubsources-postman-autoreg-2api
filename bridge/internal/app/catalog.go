// catalog.go — каталог моделей Postman AI.
//
// У Postman модели адресуются enum-ключами (GPT_56_SOL, CLAUDE_OPUS_48_BEDROCK, …),
// а клиенты привыкли к OpenAI-именам (gpt-5.6-sol, claude-opus-4.8). Здесь —
// маппинг и /v1/models.
package app

import (
	"net/http"
	"strings"
	"time"
)

type modelEntry struct {
	Alias string
	Key   string
	Name  string
}

// postmanModels — первый элемент — дефолт при пустом/неизвестном имени.
//
// ⚠️ Enum-ключи привязаны к версии Postman и могут меняться. Если модель
// отвечает ошибкой «unknown model» — проверь актуальный ключ в клиенте:
// DevTools → Network → gateway.postman.com/chat → поле model.
var postmanModels = []modelEntry{
	{"gpt-6.1-sol", "GPT_61_SOL", "GPT-6.1 Sol"},
	{"gpt-5.6-sol", "GPT_56_SOL", "GPT-5.6 Sol"},
	{"gpt-5.6-terra", "GPT_56_TERRA", "GPT-5.6 Terra"},
	{"gpt-5.6-luna", "GPT_56_LUNA", "GPT-5.6 Luna"},
	{"gpt-5.5", "GPT_55", "GPT-5.5"},
	{"gpt-5.4", "GPT_54", "GPT-5.4"},
	{"claude-opus-4.8", "CLAUDE_OPUS_48_BEDROCK", "Claude Opus 4.8"},
	{"claude-opus-4.7", "CLAUDE_OPUS_47_BEDROCK", "Claude Opus 4.7"},
	{"claude-opus-4.5", "CLAUDE_OPUS_45_BEDROCK", "Claude Opus 4.5"},
	{"claude-sonnet-4.6", "CLAUDE_46_SONNET_BEDROCK", "Claude Sonnet 4.6"},
	{"claude-sonnet-4.5", "CLAUDE_45_SONNET_BEDROCK", "Claude Sonnet 4.5"},
	{"claude-haiku-4.5", "CLAUDE_45_HAIKU_BEDROCK", "Claude Haiku 4.5"},
}

// короткие алиасы для CLI-клиентов
var shortAliases = map[string]string{
	"gpt-6.1":    "GPT_61_SOL",
	"gpt-5.6":    "GPT_56_SOL",
	"gpt-5":      "GPT_54",
	"opus-4.8":   "CLAUDE_OPUS_48_BEDROCK",
	"opus-4.7":   "CLAUDE_OPUS_47_BEDROCK",
	"opus-4.5":   "CLAUDE_OPUS_45_BEDROCK",
	"sonnet-4.6": "CLAUDE_46_SONNET_BEDROCK",
	"sonnet-4.5": "CLAUDE_45_SONNET_BEDROCK",
	"haiku-4.5":  "CLAUDE_45_HAIKU_BEDROCK",
}

// resolveModelKey приводит имя модели к ключу Postman.
// Неизвестное имя → дефолт; сырой ключ (GPT_56_TERRA) проходит как есть.
func resolveModelKey(name string) string {
	n := strings.TrimSpace(name)
	if n == "" {
		return postmanModels[0].Key
	}
	lower := strings.ToLower(n)
	for _, m := range postmanModels {
		if lower == m.Alias || n == m.Key {
			return m.Key
		}
	}
	if key, ok := shortAliases[lower]; ok {
		return key
	}
	if isPostmanKey(n) {
		return n
	}
	return postmanModels[0].Key
}

// isPostmanKey — похоже на сырой enum Postman (GPT_56_TERRA).
func isPostmanKey(s string) bool {
	if len(s) < 4 || len(s) > 44 || s[0] < 'A' || s[0] > 'Z' {
		return false
	}
	for _, r := range s {
		ok := (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '_'
		if !ok {
			return false
		}
	}
	return true
}

// resolveModel — имя из запроса клиента (пустое → DEFAULT_MODEL).
func (s *Server) resolveModel(name string) string {
	if strings.TrimSpace(name) == "" {
		name = s.cfg.DefaultModel
	}
	return resolveModelKey(name)
}

// displayName — человекочитаемое имя для ответов и дашборда.
func displayName(key string) string {
	for _, m := range postmanModels {
		if m.Key == key {
			return m.Name
		}
	}
	return key
}

// handleModels — /v1/models.
func (s *Server) handleModels(w http.ResponseWriter, r *http.Request) {
	created := time.Now().Unix()
	defaultKey := s.resolveModel("")
	data := make([]any, 0, len(postmanModels))
	for _, m := range postmanModels {
		entry := map[string]any{
			"id":          m.Alias,
			"object":      "model",
			"created":     created,
			"owned_by":    "postman",
			"name":        m.Name,
			"postman_key": m.Key,
		}
		if m.Key == defaultKey {
			entry["default"] = true
		}
		data = append(data, entry)
	}
	writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": data})
}
