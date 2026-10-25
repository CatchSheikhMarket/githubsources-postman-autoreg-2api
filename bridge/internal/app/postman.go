// postman.go — клиент gateway.postman.com.
//
// Особенности апстрима, которые ломают наивный бридж:
//   - авторизация — заголовок x-access-token (не Bearer);
//   - ответ всегда SSE-поток, даже когда стрим клиенту не нужен;
//   - input.query жёстко ограничен ~10 000 символов (это лимит поля, а не
//     контекст модели), поэтому история собирается с обрезкой, а system-текст
//     уходит в backgroundContext.AGENTS_MD без лимита;
//   - платформа, product и хэши инструментов — часть контракта запроса и
//     привязаны к версии Postman.
package app

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"
)

// Upstream — HTTP-клиент к gateway.postman.com.
type Upstream struct {
	cfg      *Config
	hc       *http.Client
	sessions *SessionPool

	// Диалог живёт на аккаунте, который его начал: продолжение
	// (USER_CONTINUE_LOOP) и TOOL_RESPONSE должны идти той же сессией.
	convMu   sync.Mutex
	convSess map[string]convSessionEntry
}

func NewUpstream(cfg *Config) *Upstream {
	return &Upstream{
		cfg:      cfg,
		sessions: NewSessionPool(cfg.SessionsFile, cfg.Token, cfg.SidCookie),
		convSess: make(map[string]convSessionEntry),
		hc: &http.Client{
			// общего Timeout нет: длинные SSE-стримы режутся им целиком.
			// Вместо него — watchdog тишины в postChat (idleBody).
			Transport: &http.Transport{
				MaxIdleConns:        64,
				MaxIdleConnsPerHost: 32,
				IdleConnTimeout:     90 * time.Second,
			},
		},
	}
}

// convSessionEntry — владелец диалога со временем последнего хода (TTL 3 ч,
// как обещано в README): без TTL карта копится бесконечно.
type convSessionEntry struct {
	sess Session
	at   time.Time
}

const convSessionTTL = 3 * time.Hour

// rememberConvSession запоминает, какой сессией обслужен диалог; протухшие
// записи вычищаются, при переполнении карта сбрасывается.
func (u *Upstream) rememberConvSession(convID string, sess Session) {
	u.convMu.Lock()
	defer u.convMu.Unlock()
	for id, e := range u.convSess {
		if time.Since(e.at) > convSessionTTL {
			delete(u.convSess, id)
		}
	}
	if len(u.convSess) > 4096 {
		u.convSess = make(map[string]convSessionEntry)
	}
	u.convSess[convID] = convSessionEntry{sess: sess, at: time.Now()}
}

func (u *Upstream) sessionForConv(convID string) (Session, bool) {
	u.convMu.Lock()
	defer u.convMu.Unlock()
	e, ok := u.convSess[convID]
	if !ok {
		return Session{}, false
	}
	if time.Since(e.at) > convSessionTTL {
		delete(u.convSess, convID)
		return Session{}, false
	}
	return e.sess, true
}

// ── Сборка input.query ──

// ChatTurn — одно сообщение диалога для input.query.
type ChatTurn struct {
	Role string
	Text string
}

// buildQuery собирает input.query из истории диалога.
//
// Postman отклоняет запросы, где query длиннее ~10k символов, поэтому:
//   - system-инструкции в query не дублируются — они уходят в
//     backgroundContext.AGENTS_MD (без лимита). Попытка продублировать их в
//     query префиксом "[System instructions]" стабильно упирается в safety-flag
//     апстрима ("This message got flagged by our safety checks");
//   - середина истории отбрасывается с маркером [...earlier messages omitted...];
//   - последний ход пользователя сохраняется приоритетно.
func buildQuery(turns []ChatTurn, system string, maxBytes int) string {
	_ = system // система передаётся через AGENTS_MD, см. комментарий выше

	var b strings.Builder

	// Последний пользовательский ход — самый важный: резервируем место.
	lastUser := ""
	histEnd := len(turns)
	for i := len(turns) - 1; i >= 0; i-- {
		if turns[i].Role == "user" {
			lastUser = strings.TrimSpace(turns[i].Text)
			histEnd = i
			break
		}
	}

	histBudget := maxBytes - len(b.String()) - len(lastUser) - 100
	if histBudget < 200 {
		histBudget = 200
	}

	// Свежие сообщения важнее старых — набираем историю с конца.
	lines := make([]string, 0, histEnd)
	for _, t := range turns[:histEnd] {
		if text := strings.TrimSpace(t.Text); text != "" {
			lines = append(lines, t.Role+": "+text)
		}
	}
	parts := make([]string, 0, len(lines))
	used := 0
	omitted := false
	for i := len(lines) - 1; i >= 0; i-- {
		need := len(lines[i]) + 2
		if used+need > histBudget {
			omitted = true
			break
		}
		parts = append([]string{lines[i]}, parts...)
		used += need
	}
	if len(parts) > 0 {
		if omitted {
			b.WriteString("[...earlier messages omitted...]\n\n")
		}
		b.WriteString(strings.Join(parts, "\n\n"))
		b.WriteString("\n\n")
	}

	if lastUser != "" {
		remain := maxBytes - len(b.String())
		if remain < 500 {
			remain = 500
		}
		b.WriteString(truncateBytes(lastUser, remain))
	}

	q := strings.TrimSpace(b.String())
	if q == "" {
		q = "Hello"
	}
	return q
}

// truncateBytes режет строку по байтам, не разрывая UTF-8 руну.
func truncateBytes(s string, max int) string {
	if max <= 0 || len(s) <= max {
		return s
	}
	cut := s[:max]
	for len(cut) > 0 && !utf8.ValidString(cut) {
		cut = cut[:len(cut)-1]
	}
	return cut + "…"
}

// ── Тело запроса к Postman ──

// ToolDef — инструмент, объявленный клиентом (OpenAI tools / Anthropic tools).
type ToolDef struct {
	Name        string
	Description string
	Parameters  map[string]any
}

// ToolResult — результат выполнения инструмента, возвращаемый в Postman.
type ToolResult struct {
	CallID  string // id из toolCall-события (как его выдал Postman)
	Content string
	Status  string // пусто = SUCCESS; иначе SUCCESS / FAILED / REJECTED
	Summary string
}

// ChatRequest — то, что мост отправляет в Postman.
type ChatRequest struct {
	Model          string // ключ Postman (GPT_56_SOL и т.п.)
	Query          string
	SystemContext  string // полный system-текст → backgroundContext.AGENTS_MD
	ConversationID string
	Thinking       bool

	Tools       []ToolDef    // клиентские инструменты → clientTools.thirdParty
	ToolResults []ToolResult // непусто → chatType TOOL_RESPONSE
	ToolGroupID string       // группа вызовов из реестра (параллельные результаты)
}

// ── Имена инструментов ──
//
// Postman работает с клиентскими инструментами в namespace "external-client":
// при отправке имя оборачивается в client__<safe>, при возврате приходит
// закодированным (иногда дополнительно обёрнутым в executeNamespaceTool).
// Мост кодирует и раскодирует имена сам — клиент видит свои родные имена.

func encodeToolName(name string) string {
	safe := strings.Map(func(r rune) rune {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '_', r == '-':
			return r
		default:
			return '_'
		}
	}, name)
	enc := "client__" + safe
	if len(enc) > 64 {
		enc = enc[:55] + "_" + shortHash(name)
	}
	return enc
}

// ToolNameMap — encoded → оригинальное имя (для декодирования ответных вызовов).
func ToolNameMap(tools []ToolDef) map[string]string {
	m := make(map[string]string, len(tools))
	for _, t := range tools {
		m[encodeToolName(t.Name)] = t.Name
	}
	return m
}

// decodeToolName раскодирует имя из toolCall-события: разворачивает обёртку
// executeNamespaceTool (вложенный input становится аргументами) и снимает
// префикс client__, если точного соответствия в объявлениях нет.
func decodeToolName(encoded, arguments string, names map[string]string) (string, string) {
	if encoded == "executeNamespaceTool" {
		var wrap struct {
			Namespace string          `json:"namespace"`
			ToolName  string          `json:"toolName"`
			Input     json.RawMessage `json:"input"`
		}
		if json.Unmarshal([]byte(arguments), &wrap) == nil &&
			wrap.Namespace == "external-client" && wrap.ToolName != "" {
			inner := rawToText(wrap.Input)
			if strings.TrimSpace(inner) == "" {
				inner = "{}"
			}
			return decodeToolName(wrap.ToolName, inner, names)
		}
	}
	if name, ok := names[encoded]; ok {
		return name, arguments
	}
	if trimmed := strings.TrimPrefix(encoded, "client__"); trimmed != "" && trimmed != encoded {
		return trimmed, arguments
	}
	return encoded, arguments
}

func shortHash(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:4])
}

// Дефолты полей toolResponse для Postman.
func toolStatus(r ToolResult) string {
	if r.Status == "" {
		return "SUCCESS"
	}
	return r.Status
}

func toolSummary(r ToolResult) string {
	if r.Summary != "" {
		return r.Summary
	}
	if toolStatus(r) == "SUCCESS" {
		return "Client tool completed"
	}
	return "Client tool did not complete successfully"
}

func toolContent(r ToolResult) string {
	if strings.TrimSpace(r.Content) == "" {
		return "(tool completed with no output)"
	}
	return r.Content
}

// buildBody собирает тело POST /chat.
//
// С клиентскими инструментами Postman работает только в product
// workspace_tools_sdk_localmode: иначе thirdParty-набор игнорируется.
// Когда передаются результаты инструментов — chatType TOOL_RESPONSE того же
// диалога (conversationId обязателен, иначе апстрим отвечает ошибкой).
func (u *Upstream) buildBody(req *ChatRequest) map[string]any {
	product := u.cfg.Product
	toolsHash := u.cfg.ToolsHash
	termsHash := u.cfg.TermsHash
	if len(req.Tools) > 0 || len(req.ToolResults) > 0 {
		product = u.cfg.ProductTools
		toolsHash = u.cfg.ToolsHashSDK
		termsHash = u.cfg.TermsHashSDK
	}

	background := []any{
		map[string]any{"type": "ACTIVE_ENVIRONMENT", "value": nil},
		map[string]any{"type": "ACTIVE_WORKSPACE", "value": map[string]any{
			"name": "Proxy Workspace",
			"id":   u.cfg.WorkspaceID,
		}},
	}
	if req.SystemContext != "" {
		background = append(background, map[string]any{
			"type":  "AGENTS_MD",
			"value": map[string]any{"agentsMdFileContent": req.SystemContext},
		})
	}

	input := map[string]any{
		"chatType":       "USER_QUERY",
		"query":          req.Query,
		"toolResponse":   "",
		"useCase":        nil,
		"conversationId": nullable(req.ConversationID),
		"agent":          nil,
		"product":        product,
		"startedFrom":    "CHAT_INPUT",
	}
	if len(req.ToolResults) > 0 {
		delete(input, "startedFrom")
		input["chatType"] = "TOOL_RESPONSE"
		input["query"] = ""
		if req.ToolGroupID != "" {
			responses := make([]any, 0, len(req.ToolResults))
			for _, r := range req.ToolResults {
				item := map[string]any{
					"toolCallId":          r.CallID,
					"content":             toolContent(r),
					"toolResponseSummary": toolSummary(r),
					"toolResponseStatus":  toolStatus(r),
				}
				if toolStatus(r) == "REJECTED" {
					item["toolResponseRejectionType"] = "EXPLICIT"
				}
				responses = append(responses, item)
			}
			input["toolCallGroupId"] = req.ToolGroupID
			input["toolResponses"] = responses
		} else {
			r := req.ToolResults[0]
			input["toolCallId"] = r.CallID
			input["toolResponse"] = toolContent(r)
			input["toolResponseSummary"] = toolSummary(r)
			input["toolResponseStatus"] = toolStatus(r)
		}
	}

	clientTools := map[string]any{
		"native":          []any{},
		"excludedTools":   []any{"askUser"},
		"nativeToolsHash": toolsHash,
	}
	if len(req.Tools) > 0 {
		defs := make([]any, 0, len(req.Tools))
		for _, t := range req.Tools {
			params := t.Parameters
			if params == nil {
				params = map[string]any{"type": "object", "properties": map[string]any{}}
			}
			defs = append(defs, map[string]any{
				"name":        encodeToolName(t.Name),
				"description": t.Description,
				"parameters":  params,
			})
		}
		clientTools["thirdParty"] = map[string]any{
			"external-client": map[string]any{"tools": defs},
		}
	}

	return map[string]any{
		"input":       input,
		"platform":    u.cfg.Platform,
		"clientTools": clientTools,
		"clientKBTerms": map[string]any{
			"nativeTermsHash": termsHash,
			"excludedKBTerms": []any{},
		},
		"mandatoryContext":  map[string]any{"workspaceId": u.cfg.WorkspaceID},
		"selectedContext":   []any{},
		"backgroundContext": background,
		"devModeOptions": map[string]any{
			"selectedModel":                  req.Model,
			"isParallelToolCallingSupported": true,
			"autoRun":                        false,
			"supportsAskUser":                false,
			"supportsActionRecommendations":  true,
			"useThinkingModeIfAvailable":     req.Thinking,
			"thinkingLevel":                  "medium",
			"isAgentModeEnabled":             true,
		},
	}
}

func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// ── SSE-события апстрима ──

// PMEvent — конверт события: {"eventType": "...", "data": {...}}.
type PMEvent struct {
	EventType string `json:"eventType"`
	Data      PMData `json:"data"`
}

// PMData — union полей data разных типов событий
// (textChunk / thinkingChunk / conversation / usage / failure / toolCall).
type PMData struct {
	TextContent     string       `json:"textContent"`
	ThinkingContent string       `json:"thinkingContent"`
	Thinking        string       `json:"thinking"`
	ID              string       `json:"id"` // conversationId
	ModelKey        string       `json:"modelKey"`
	UserType        string       `json:"userType"`
	UsageState      string       `json:"usageState"`
	Limit           int          `json:"limit"`
	Usage           int          `json:"usage"`
	ErrorType       string       `json:"errorType"`
	UserMessage     string       `json:"userMessage"`
	Message         string       `json:"message"`
	ToolCallID      string       `json:"toolCallId"`
	ToolName        string       `json:"toolName"`
	ToolCalls       []PMToolCall `json:"toolCalls"`
	Metadata        struct {
		ID string `json:"id"`
	} `json:"metadata"`
}

// PMToolCall — элемент data.toolCalls в событиях toolCall / toolCallChunk.
type PMToolCall struct {
	ID              string          `json:"id"`
	ToolCallID      string          `json:"toolCallId"`
	GroupID         string          `json:"groupId"`
	ToolCallGroupID string          `json:"toolCallGroupId"`
	Name            string          `json:"name"`
	Arguments       json.RawMessage `json:"arguments"`
	Function        *struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	} `json:"function"`
}

func (c *PMToolCall) callID() string {
	if c.ID != "" {
		return c.ID
	}
	return c.ToolCallID
}

func (c *PMToolCall) groupID() string {
	if c.ToolCallGroupID != "" {
		return c.ToolCallGroupID
	}
	return c.GroupID
}

func (c *PMToolCall) fnName() string {
	if c.Function != nil && c.Function.Name != "" {
		return c.Function.Name
	}
	return c.Name
}

func (c *PMToolCall) fnArgs() string {
	raw := c.Arguments
	if c.Function != nil && len(c.Function.Arguments) > 0 {
		raw = c.Function.Arguments
	}
	return rawToText(raw)
}

// rawToText: строка возвращается как есть (это JSON-текст аргументов),
// объект/массив — сериализуется обратно.
func rawToText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	return string(raw)
}

// switchAgentSeparator — служебный разделитель смены агента в текстовом потоке.
func (d PMData) switchAgentSeparator() bool {
	return d.Metadata.ID == "switch-agent-separator"
}

// ── Накопление tool-вызовов ──

// resolvedTool — готовый к отдаче клиенту tool call.
type resolvedTool struct {
	ID        string
	GroupID   string
	Name      string // уже раскодированное клиентское имя
	Arguments string
}

// toolCollector собирает toolCall/toolCallChunk события: полный вызов приходит
// один раз (toolCall), аргументы чанками — дельтами (toolCallChunk).
type toolCollector struct {
	order []string
	byID  map[string]*toolPart
}

type toolPart struct {
	id      string
	groupID string
	encoded string
	args    strings.Builder
}

func newToolCollector() *toolCollector {
	return &toolCollector{byID: map[string]*toolPart{}}
}

func (tc *toolCollector) merge(data PMData, chunk bool) {
	for i := range data.ToolCalls {
		call := &data.ToolCalls[i]
		id := call.callID()
		if id == "" {
			continue
		}
		part := tc.byID[id]
		if part == nil {
			part = &toolPart{id: id}
			tc.byID[id] = part
			tc.order = append(tc.order, id)
		}
		if g := call.groupID(); g != "" {
			part.groupID = g
		}
		if n := call.fnName(); n != "" {
			part.encoded = n
		}
		args := call.fnArgs()
		if chunk {
			part.args.WriteString(args)
		} else {
			part.args.Reset()
			part.args.WriteString(args)
		}
	}
}

// isClientToolEncoded — вызов инструмента, объявленного клиентом? Апстрим
// возвращает thirdParty-вызовы с префиксом client__ или в обёртке
// executeNamespaceTool; всё остальное — нативные инструменты агента Postman
// (listDirectory, readFile…), которые мост обязан обработать сам, а не
// пробрасывать клиенту.
func isClientToolEncoded(encoded string, names map[string]string) bool {
	if _, ok := names[encoded]; ok {
		return true
	}
	if strings.HasPrefix(encoded, "client__") {
		return true
	}
	return encoded == "executeNamespaceTool"
}

// list отдаёт собранные вызовы с раскодированными именами. Нативные вызовы
// апстрима (listDirectory, readFile…) переводятся в клиентские инструменты
// (nativetools.go); непереводимые не возвращаются — их обрабатывает сам мост
// (streamAgentTurns отвечает отказом от имени клиента).
func (tc *toolCollector) list(tools []ToolDef) []resolvedTool {
	names := ToolNameMap(tools)
	client := newClientToolSet(tools)
	out := make([]resolvedTool, 0, len(tc.order))
	for _, id := range tc.order {
		p := tc.byID[id]
		args := strings.TrimSpace(p.args.String())
		if args == "" {
			args = "{}"
		}
		if isClientToolEncoded(p.encoded, names) {
			name, a := decodeToolName(p.encoded, args, names)
			out = append(out, resolvedTool{ID: p.id, GroupID: p.groupID, Name: name, Arguments: a})
			continue
		}
		if needs, ok := nativeToolNeeds(p.encoded); ok && client.hasAny(needs...) {
			name, a := translateNativeCall(p.encoded, parsedArgs(args), client)
			raw, _ := json.Marshal(a)
			out = append(out, resolvedTool{ID: p.id, GroupID: p.groupID, Name: name, Arguments: string(raw)})
		}
	}
	return out
}

// StreamChat — POST /chat. Апстрим всегда отвечает SSE; callback возвращает
// false, чтобы прекратить чтение. Токены, отбитые 401, уходят в отставку, и
// запрос повторяется со следующего аккаунта. Запросы с conversationId
// (TOOL_RESPONSE, продолжение диалога) идут сессией-владельцем диалога:
// чужой аккаунт отвечает «couldn't find this chat».
func (u *Upstream) StreamChat(ctx context.Context, req *ChatRequest, onEvent func(PMEvent) bool) error {
	body := u.buildBody(req)
	_, total := u.sessions.Stats()
	if total == 0 {
		return fmt.Errorf("нет токенов Postman: войди в Postman Desktop или заполни %s", u.cfg.SessionsFile)
	}
	if req.ConversationID != "" {
		if sess, ok := u.sessionForConv(req.ConversationID); ok {
			resp, err := u.postChat(ctx, sess, body)
			if err != nil {
				return err
			}
			if resp.StatusCode == http.StatusUnauthorized {
				// владелец диалога протух — ретирим и падаем в ротацию
				resp.Body.Close()
				u.sessions.Retire(sess.Value)
			} else {
				return u.consumeChat(resp, sess, onEvent)
			}
		}
	}
	var lastAPI *APIError
	for range total {
		sess, ok := u.sessions.Next()
		if !ok {
			break
		}
		resp, err := u.postChat(ctx, sess, body)
		if err != nil {
			return err
		}
		if resp.StatusCode == http.StatusUnauthorized {
			resp.Body.Close()
			u.sessions.Retire(sess.Value)
			continue
		}
		if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500 {
			// временная ошибка аккаунта — переходим к следующему,
			// последнюю ошибку отдаём после перебора
			raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
			resp.Body.Close()
			lastAPI = &APIError{Status: resp.StatusCode, Body: snippet(raw)}
			continue
		}
		return u.consumeChat(resp, sess, onEvent)
	}
	if lastAPI != nil {
		return lastAPI
	}
	return fmt.Errorf("все аккаунты Postman отбиты 401 — обнови токены")
}

// consumeChat доводит ответ апстрима до конца: не-200 → APIError, иначе SSE
// с запоминанием сессии-владельца диалога.
func (u *Upstream) consumeChat(resp *http.Response, sess Session, onEvent func(PMEvent) bool) error {
	if resp.StatusCode != http.StatusOK {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		resp.Body.Close()
		return &APIError{Status: resp.StatusCode, Body: snippet(raw)}
	}
	err := parseSSE(resp.Body, func(ev PMEvent) bool {
		if ev.EventType == "conversation" && ev.Data.ID != "" {
			u.rememberConvSession(ev.Data.ID, sess)
		}
		return onEvent(ev)
	})
	resp.Body.Close()
	return err
}

// ── Авто-продолжение лупа (loopApprovalChunk) ──

// continueBody — тело продолжения: конверт обычного запроса, но input
// переключён на USER_CONTINUE_LOOP.
func (u *Upstream) continueBody(req *ChatRequest, convID string) map[string]any {
	body := u.buildBody(req)
	in := body["input"].(map[string]any)
	in["chatType"] = "USER_CONTINUE_LOOP"
	in["query"] = ""
	in["conversationId"] = convID
	delete(in, "startedFrom")
	for _, k := range []string{"toolResponse", "toolCallId", "toolResponses", "toolCallGroupId", "toolResponseSummary", "toolResponseRejectionType"} {
		delete(in, k)
	}
	return body
}

// ContinueLoop — один ход продолжения диалога. Идёт сессией-владельцем
// диалога; если владелец отбился 401 — ретирим и честно падаем в ротацию
// (тот же фейл-овер, что в StreamChat).
func (u *Upstream) ContinueLoop(ctx context.Context, req *ChatRequest, convID string, onEvent func(PMEvent) bool) error {
	body := u.continueBody(req, convID)
	sess, ok := u.sessionForConv(convID)
	if !ok {
		sess, ok = u.sessions.Next()
	}
	if !ok {
		return fmt.Errorf("нет живых аккаунтов Postman")
	}
	resp, err := u.postChat(ctx, sess, body)
	if err != nil {
		return err
	}
	if resp.StatusCode == http.StatusUnauthorized {
		resp.Body.Close()
		u.sessions.Retire(sess.Value)
		_, total := u.sessions.Stats()
		for range total {
			next, ok := u.sessions.Next()
			if !ok {
				break
			}
			resp, err = u.postChat(ctx, next, body)
			if err != nil {
				return err
			}
			if resp.StatusCode == http.StatusUnauthorized {
				resp.Body.Close()
				u.sessions.Retire(next.Value)
				continue
			}
			return u.consumeChat(resp, next, onEvent)
		}
		return fmt.Errorf("все аккаунты Postman отбиты 401 — обнови токены")
	}
	return u.consumeChat(resp, sess, onEvent)
}

// rejectTools отвечает TOOL_RESPONSE с отказом по чужим вызовам (нативные
// инструменты апстрима, которые не удалось перевести в клиентские). Возвращает
// rejected=true, если апстрим отверг сам отказ («I lost my way»): тогда ход
// завершается накопленным текстом, клиент этой ошибки не увидит.
func (u *Upstream) rejectTools(ctx context.Context, req *ChatRequest, convID string, results []ToolResult, groupID string, onEvent func(PMEvent) bool) (bool, error) {
	respond := func(results []ToolResult, groupID string) (bool, error) {
		creq := *req
		creq.ConversationID = convID
		creq.ToolResults = results
		creq.ToolGroupID = groupID
		failed := false
		wrapped := func(ev PMEvent) bool {
			if ev.EventType == "failure" {
				failed = true
				return false
			}
			return onEvent(ev)
		}
		sess, ok := u.sessionForConv(convID)
		if !ok {
			sess, ok = u.sessions.Next()
		}
		if !ok {
			return false, fmt.Errorf("нет живых аккаунтов Postman")
		}
		resp, err := u.postChat(ctx, sess, u.buildBody(&creq))
		if err != nil {
			return false, err
		}
		if resp.StatusCode == http.StatusUnauthorized {
			// владелец диалога протух — ретирим и пробуем следующего,
			// как в StreamChat (иначе отказ по тулам умирает навсегда)
			resp.Body.Close()
			u.sessions.Retire(sess.Value)
			next, ok := u.sessions.Next()
			if !ok {
				return false, fmt.Errorf("все аккаунты Postman отбиты 401 — обнови токены")
			}
			resp, err = u.postChat(ctx, next, u.buildBody(&creq))
			if err != nil {
				return false, err
			}
			sess = next
		}
		if err := u.consumeChat(resp, sess, wrapped); err != nil {
			return failed, err
		}
		return failed, nil
	}

	if groupID != "" {
		return respond(results, groupID)
	}
	for _, r := range results {
		if err := ctx.Err(); err != nil {
			return false, err
		}
		failed, err := respond([]ToolResult{r}, "")
		if err != nil || failed {
			return failed, err
		}
	}
	return false, nil
}

// maxAgentTurns — предохранитель на число служебных ходов моста за один
// клиентский запрос (продолжения лупа + отказы по чужим вызовам).
const maxAgentTurns = 24

// isChatLost — апстрим сообщает, что диалог не найден (истёк, потерян или
// запрос попал не на аккаунт-владелец).
func isChatLost(d PMData) bool {
	msg := strings.ToLower(strings.Join([]string{d.UserMessage, d.Message, d.ErrorType}, " "))
	return strings.Contains(msg, "find this chat")
}

// foldToolResults вшивает результаты инструментов в текст запроса — для
// старта в новом диалоге, когда апстрим потерял прежний.
func foldToolResults(results []ToolResult, prev string, maxBytes int) string {
	var b strings.Builder
	b.WriteString("The previous conversation was lost on the server. The client already executed the requested tools; " +
		"their results are below. Continue the task from here.\n\n")
	if strings.TrimSpace(prev) != "" {
		b.WriteString(strings.TrimSpace(prev))
		b.WriteString("\n\n")
	}
	for _, r := range results {
		fmt.Fprintf(&b, "Tool call %s — status %s:\n%s\n\n", r.CallID, toolStatus(r), truncateBytes(toolContent(r), 2500))
	}
	return truncateBytes(strings.TrimSpace(b.String()), maxBytes)
}

// streamAgentTurns ведёт агентный цикл одного клиентского запроса: основной
// стрим, авто-продолжение лупа (loopApprovalChunk) и авто-отказ по чужим
// вызовам — нативным инструментам апстрима, которые не удалось перевести в
// клиентские (мост отвечает FAILED от имени клиента, клиент их не видит).
// Возвращает признак потери диалога для ретрая свежим диалогом.
func (u *Upstream) streamAgentTurns(ctx context.Context, req *ChatRequest, onEvent func(PMEvent) bool) (bool, error) {
	names := ToolNameMap(req.Tools)
	client := newClientToolSet(req.Tools)
	convID := ""
	paused := false
	chatLost := false
	var foreignIDs []string
	foreign := map[string]string{}
	foreignGroup := map[string]string{}

	handle := func(ev PMEvent) bool {
		switch ev.EventType {
		case "conversation":
			if ev.Data.ID != "" {
				convID = ev.Data.ID
			}
		case "loopApprovalChunk":
			paused = true
			return true
		case "failure":
			if isChatLost(ev.Data) {
				chatLost = true
				return false
			}
		case "toolCall", "toolCallChunk":
			for i := range ev.Data.ToolCalls {
				call := &ev.Data.ToolCalls[i]
				id := call.callID()
				encoded := call.fnName()
				if id == "" || isClientToolEncoded(encoded, names) {
					continue
				}
				if needs, ok := nativeToolNeeds(encoded); ok && client.hasAny(needs...) {
					continue // переводится в клиентский инструмент в list()
				}
				if _, ok := foreign[id]; !ok {
					foreignIDs = append(foreignIDs, id)
				}
				foreign[id] = encoded
				if g := call.groupID(); g != "" {
					foreignGroup[id] = g
				}
			}
		}
		return onEvent(ev)
	}

	err := u.StreamChat(ctx, req, handle)
	for i := 0; err == nil && i < maxAgentTurns; i++ {
		if paused {
			paused = false
			if convID == "" {
				break
			}
			err = u.ContinueLoop(ctx, req, convID, handle)
			continue
		}
		if len(foreignIDs) > 0 {
			if convID == "" {
				break
			}
			results := make([]ToolResult, 0, len(foreignIDs))
			group := ""
			if g, ok := foreignGroup[foreignIDs[0]]; ok && g != "" {
				same := true
				for _, id := range foreignIDs {
					if foreignGroup[id] != g {
						same = false
						break
					}
				}
				if same {
					group = g
				}
			}
			for _, id := range foreignIDs {
				results = append(results, ToolResult{
					CallID: id,
					Content: fmt.Sprintf("Tool %q is not available in this environment and cannot be executed. "+
						"Use only the tools declared by the client.", foreign[id]),
					Status:  "FAILED",
					Summary: "Tool is not available",
				})
			}
			log.Printf("[postman2api] агент вызвал нативный инструмент не из клиентского набора (%s, group=%q) — мост отвечает отказом и продолжает диалог",
				strings.Join(foreignIDs, ", "), group)
			foreignIDs = nil
			foreign = map[string]string{}
			foreignGroup = map[string]string{}
			rejected, rerr := u.rejectTools(ctx, req, convID, results, group, handle)
			if rejected {
				// апстрим отверг отказ по нативному вызову — завершаем ход
				// накопленным текстом вместо ошибки клиенту
				log.Printf("[postman2api] апстрим отверг отказ по нативному вызову — завершаю ход накопленным ответом")
				break
			}
			err = rerr
			continue
		}
		break
	}
	return chatLost, err
}

// StreamChatAuto — стрим с полной агентной обработкой: продолжения лупа и
// отказы по чужим вызовам (streamAgentTurns) плюс восстановление после
// потери диалога апстримом — повтор запроса свежим диалогом, с результатами
// инструментов, вшитыми в текст.
func (u *Upstream) StreamChatAuto(ctx context.Context, req *ChatRequest, onEvent func(PMEvent) bool) error {
	lost, err := u.streamAgentTurns(ctx, req, onEvent)
	if !lost {
		return err
	}
	retry := *req
	retry.ConversationID = ""
	if len(retry.ToolResults) > 0 {
		retry.Query = foldToolResults(retry.ToolResults, retry.Query, u.cfg.MaxQuery)
		retry.ToolResults = nil
		retry.ToolGroupID = ""
	}
	log.Printf("[postman2api] диалог %q потерян апстримом — повторяю запрос свежим диалогом", req.ConversationID)
	lost, err = u.streamAgentTurns(ctx, &retry, onEvent)
	if lost && err == nil {
		onEvent(PMEvent{EventType: "failure", Data: PMData{
			UserMessage: "Looks like I couldn't find this chat. Try creating a new one.",
		}})
	}
	return err
}

func (u *Upstream) postChat(ctx context.Context, sess Session, body map[string]any) (*http.Response, error) {
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	// собственный cancel на каждый вызов: watchdog тишины ниже гасит запрос,
	// если апстрим замолчал посреди стрима
	callCtx, cancel := context.WithCancel(ctx)
	req, err := http.NewRequestWithContext(callCtx, http.MethodPost, u.cfg.BaseURL+"/chat", bytes.NewReader(raw))
	if err != nil {
		cancel()
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	if sess.Cookie {
		// Веб-сессия: gateway принимает куку postman.sid как авторизацию.
		req.Header.Set("Cookie", "postman.sid="+sess.Value)
	} else {
		req.Header.Set("x-access-token", sess.Value)
	}
	req.Header.Set("x-pstmn-req-service", "agent-mode-service")
	req.Header.Set("x-app-version", u.cfg.AppVersion)
	req.Header.Set("Origin", "https://desktop.postman.com")
	req.Header.Set("Referer", "https://desktop.postman.com/")
	req.Header.Set("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "+
		"(KHTML, like Gecko) Postman/"+u.cfg.AppVersion+" Electron/37.10.3 Safari/537.36")
	resp, err := u.hc.Do(req)
	if err != nil {
		cancel()
		return nil, err
	}
	// watchdog тишины: стрим без данных дольше cfg.Timeout — запрос гасится.
	// Общий таймаут на стрим не ставим: долгий ход не должен обрываться.
	resp.Body = newIdleBody(resp.Body, time.Duration(u.cfg.Timeout)*time.Second, cancel)
	return resp, nil
}

// idleBody — тело ответа с watchdog'ом: если апстрим не шлёт байты дольше
// idle, вызывается cancel и Read получает ошибку контекста.
type idleBody struct {
	rc     io.ReadCloser
	idle   time.Duration
	timer  *time.Timer
	cancel context.CancelFunc
}

func newIdleBody(rc io.ReadCloser, idle time.Duration, cancel context.CancelFunc) *idleBody {
	if idle <= 0 {
		idle = 300 * time.Second
	}
	return &idleBody{rc: rc, idle: idle, cancel: cancel, timer: time.AfterFunc(idle, cancel)}
}

func (b *idleBody) Read(p []byte) (int, error) {
	n, err := b.rc.Read(p)
	if n > 0 {
		b.timer.Reset(b.idle)
	}
	return n, err
}

func (b *idleBody) Close() error {
	b.timer.Stop()
	b.cancel() // освобождаем ресурсы вызова
	return b.rc.Close()
}

// parseSSE разбирает поток: события разделены пустой строкой, payload — в
// строках "data: {...}".
func parseSSE(r io.Reader, onEvent func(PMEvent) bool) error {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)

	var data strings.Builder
	flush := func() bool {
		raw := strings.TrimSpace(data.String())
		data.Reset()
		if raw == "" || raw == "[DONE]" {
			return true
		}
		var ev PMEvent
		if json.Unmarshal([]byte(raw), &ev) != nil || ev.EventType == "" {
			return true
		}
		return onEvent(ev)
	}

	for sc.Scan() {
		line := sc.Text()
		if line == "" {
			if !flush() {
				return nil
			}
			continue
		}
		if strings.HasPrefix(line, "data:") {
			if data.Len() > 0 {
				data.WriteString("\n")
			}
			data.WriteString(strings.TrimSpace(strings.TrimPrefix(line, "data:")))
		}
	}
	if !flush() {
		return nil
	}
	return sc.Err()
}

// ── Ошибки и статистика ──

// APIError — ошибка апстрима (401 протухший токен, 403 consent=false, 5xx).
type APIError struct {
	Status int
	Body   string
}

func (e *APIError) Error() string {
	msg := e.Body
	var env struct {
		Error struct {
			Message string `json:"message"`
			Code    int    `json:"code"`
		} `json:"error"`
	}
	if json.Unmarshal([]byte(e.Body), &env) == nil && env.Error.Message != "" {
		msg = env.Error.Message
	}
	return fmt.Sprintf("upstream %d: %s", e.Status, msg)
}

func snippet(b []byte) string {
	s := strings.TrimSpace(string(b))
	if len(s) > 400 {
		return s[:400] + "…"
	}
	return s
}

type Stats struct {
	req int64
	err int64
}

func (s *Stats) Requests() int64 { return atomic.LoadInt64(&s.req) }
func (s *Stats) Errors() int64   { return atomic.LoadInt64(&s.err) }
func (s *Stats) incReq()         { atomic.AddInt64(&s.req, 1) }
func (s *Stats) incErr()         { atomic.AddInt64(&s.err, 1) }
