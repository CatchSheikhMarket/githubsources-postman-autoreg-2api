// chat.go — OpenAI-совместимый /v1/chat/completions.
//
// Апстрим (gateway.postman.com) отдаёт SSE со своими событиями
// (textChunk / thinkingChunk / conversation / usage / failure / toolCall).
// Здесь они пересобираются в OpenAI-формат, который понимают Cline, Roo,
// Continue.
//
// Tool-calling: определения инструментов уходят в clientTools.thirdParty
// (product workspace_tools_sdk_localmode), вызовы возвращаются событиями
// toolCall/toolCallChunk, результаты клиент присылает следующим запросом
// (role=tool) — мост превращает их в TOOL_RESPONSE того же диалога.
package app

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// ── Входные типы (OpenAI) ──

type OAIMessage struct {
	Role       string          `json:"role"`
	Content    json.RawMessage `json:"content"`
	Name       string          `json:"name,omitempty"`
	ToolCallID string          `json:"tool_call_id,omitempty"`
	ToolCalls  []OAIToolCall   `json:"tool_calls,omitempty"`
}

type OAIToolCall struct {
	ID       string      `json:"id,omitempty"`
	Type     string      `json:"type,omitempty"`
	Function OAIFunction `json:"function"`
}

type OAIFunction struct {
	Name      string `json:"name,omitempty"`
	Arguments string `json:"arguments,omitempty"`
}

// OAITool — объявление инструмента. Клиенты шлют либо вложенный function,
// либо (реже) поля верхнего уровня.
type OAITool struct {
	Type        string           `json:"type,omitempty"`
	Function    *OAIToolFunction `json:"function,omitempty"`
	Name        string           `json:"name,omitempty"`
	Description string           `json:"description,omitempty"`
	Parameters  map[string]any   `json:"parameters,omitempty"`
}

type OAIToolFunction struct {
	Name        string         `json:"name,omitempty"`
	Description string         `json:"description,omitempty"`
	Parameters  map[string]any `json:"parameters,omitempty"`
}

type OAIRequest struct {
	Model    string       `json:"model"`
	Messages []OAIMessage `json:"messages"`
	Stream   bool         `json:"stream"`
	Tools    []OAITool    `json:"tools"`

	// Продолжение того же диалога на стороне Postman.
	PostmanConversationID string `json:"_postman_conversation_id"`
	ConversationID        string `json:"conversation_id"`
}

func (r *OAIRequest) convID() string {
	if r.PostmanConversationID != "" {
		return r.PostmanConversationID
	}
	return r.ConversationID
}

// ── Конвертация в апстрим ──

// toPostman собирает ChatRequest: system-сообщения уходят в backgroundContext
// без лимита, остальная история — в query с обрезкой под лимит поля.
// Замыкающий блок role=tool превращается в TOOL_RESPONSE того же диалога.
func (s *Server) toPostman(req *OAIRequest) (*ChatRequest, error) {
	var sys strings.Builder
	turns := make([]ChatTurn, 0, len(req.Messages))

	// Результаты инструментов — только замыкающий блок: обычно это ровно
	// последние сообщения истории после assistant.tool_calls.
	toolResults := closingToolResults(req.Messages)
	inClosing := make(map[string]bool, len(toolResults))
	for _, r := range toolResults {
		inClosing[r.CallID] = true
	}

	for _, m := range req.Messages {
		text := textOf(m.Content)
		switch strings.ToLower(m.Role) {
		case "system", "developer":
			if text != "" {
				if sys.Len() > 0 {
					sys.WriteString("\n\n")
				}
				sys.WriteString(text)
			}
		case "tool":
			// Замыкающие результаты уже собраны в toolResults; остальные
			// (середина истории) остаются в query как текстовые ходы.
			if inClosing[m.ToolCallID] {
				continue
			}
			if text != "" {
				turns = append(turns, ChatTurn{Role: "tool", Text: text})
			}
		default:
			if text != "" {
				turns = append(turns, ChatTurn{Role: m.Role, Text: text})
			}
		}
	}

	pm := &ChatRequest{
		Model:          s.resolveModel(req.Model),
		SystemContext:  sys.String(),
		ConversationID: req.convID(),
		Thinking:       true,
		Tools:          parseOAITools(req.Tools),
	}
	if len(toolResults) > 0 {
		callIDs := make([]string, 0, len(toolResults))
		for _, r := range toolResults {
			callIDs = append(callIDs, r.CallID)
		}
		convID, groupID, err := s.lookupContinuation(callIDs, req.convID())
		if err != nil {
			// мост не помнит вызовы (перезапуск) — не роняем запрос клиента,
			// продолжаем свежим диалогом, вшив результаты в query.
			log.Printf("[postman2api] %v — продолжаю свежим диалогом", err)
			pm.ConversationID = ""
			pm.Query = foldToolResults(toolResults, "", s.cfg.MaxQuery)
			return pm, nil
		}
		pm.ConversationID = convID
		pm.ToolResults = toolResults
		pm.ToolGroupID = groupID
		return pm, nil
	}

	pm.Query = buildQuery(turns, sys.String(), s.cfg.MaxQuery)
	return pm, nil
}

// closingToolResults собирает tool-результаты замыкающего блока истории.
func closingToolResults(msgs []OAIMessage) []ToolResult {
	var results []ToolResult
	for i := len(msgs) - 1; i >= 0; i-- {
		if strings.ToLower(msgs[i].Role) != "tool" || msgs[i].ToolCallID == "" {
			break
		}
		results = append([]ToolResult{{
			CallID:  msgs[i].ToolCallID,
			Content: textOf(msgs[i].Content),
		}}, results...)
	}
	return results
}

// parseOAITools — определения инструментов из OpenAI-запроса.
func parseOAITools(tools []OAITool) []ToolDef {
	out := make([]ToolDef, 0, len(tools))
	for _, t := range tools {
		if t.Function != nil && t.Function.Name != "" {
			out = append(out, ToolDef{
				Name:        t.Function.Name,
				Description: t.Function.Description,
				Parameters:  t.Function.Parameters,
			})
			continue
		}
		if t.Name != "" {
			out = append(out, ToolDef{Name: t.Name, Description: t.Description, Parameters: t.Parameters})
		}
	}
	return out
}

// lookupContinuation ищет conversationId/groupId для TOOL_RESPONSE:
// сначала в самом запросе (_postman_conversation_id), затем в реестре вызовов.
func (s *Server) lookupContinuation(callIDs []string, fallbackConvID string) (string, string, error) {
	convID := fallbackConvID
	groupIDs := map[string]bool{}
	known := 0
	for _, id := range callIDs {
		rec, ok := s.tools.get(id)
		if !ok {
			continue
		}
		known++
		if convID == "" {
			convID = rec.conversationID
		}
		if rec.groupID != "" {
			groupIDs[rec.groupID] = true
		}
	}
	if convID == "" {
		return "", "", fmt.Errorf("не найден conversationId для tool-результатов: мост не помнит " +
			"эти вызовы (после перезапуска передай _postman_conversation_id из прошлого ответа)")
	}
	groupID := ""
	if known == len(callIDs) && len(groupIDs) == 1 {
		for g := range groupIDs {
			groupID = g
		}
	}
	return convID, groupID, nil
}

// rememberTools — сохранить toolCallId → conversationId/groupId для следующего тура.
func (s *Server) rememberTools(convID string, tools []resolvedTool) {
	if convID == "" {
		return
	}
	for _, t := range tools {
		s.tools.put(t.ID, convID, t.GroupID, t.Name)
	}
}

// oaiToolCalls — tool_calls в формате OpenAI.
func oaiToolCalls(tools []resolvedTool) []any {
	out := make([]any, 0, len(tools))
	for _, t := range tools {
		out = append(out, map[string]any{
			"id":   t.ID,
			"type": "function",
			"function": map[string]any{
				"name":      t.Name,
				"arguments": t.Arguments,
			},
		})
	}
	return out
}

// textOf достаёт текст из content: строка или массив text-частей.
func textOf(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		var b strings.Builder
		for _, p := range parts {
			if p.Text != "" {
				b.WriteString(p.Text)
			}
		}
		return b.String()
	}
	return ""
}

// ── Handler ──

func (s *Server) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	s.stats.incReq()

	var req OAIRequest
	// лимит тела: без MaxBytesReader один запрос вычитывает память без ограничений
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32<<20)).Decode(&req); err != nil {
		s.stats.incErr()
		writeError(w, http.StatusBadRequest, "cannot parse request: "+err.Error())
		return
	}
	if len(req.Messages) == 0 {
		writeError(w, http.StatusBadRequest, "messages is required")
		return
	}

	pm, err := s.toPostman(&req)
	if err != nil {
		s.stats.incErr()
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if !req.Stream {
		s.replyNonStream(w, r.Context(), &req, pm)
		return
	}
	s.replyStream(w, r.Context(), &req, pm)
}

// ── Сборка ответа апстрима ──

// collected — накопленный ответ Postman.
type collected struct {
	text     strings.Builder
	thinking strings.Builder
	convID   string
	modelKey string
	usage    *PMData
	failure  string
	tools    *toolCollector
}

func (s *Server) collect(ctx context.Context, pm *ChatRequest, out *collected) error {
	if out.tools == nil {
		out.tools = newToolCollector()
	}
	err := s.up.StreamChatAuto(ctx, pm, func(ev PMEvent) bool {
		switch ev.EventType {
		case "textChunk":
			if !ev.Data.switchAgentSeparator() {
				out.text.WriteString(ev.Data.TextContent)
			}
		case "thinkingChunk":
			t := ev.Data.ThinkingContent
			if t == "" {
				t = ev.Data.Thinking
			}
			out.thinking.WriteString(t)
		case "conversation":
			if ev.Data.ID != "" {
				out.convID = ev.Data.ID
			}
			if ev.Data.ModelKey != "" {
				out.modelKey = ev.Data.ModelKey
			}
		case "toolCall":
			out.tools.merge(ev.Data, false)
		case "toolCallChunk":
			out.tools.merge(ev.Data, true)
		case "usage":
			d := ev.Data
			out.usage = &d
		case "failure":
			out.failure = ev.Data.UserMessage
			if out.failure == "" {
				out.failure = ev.Data.Message
			}
			if out.failure == "" {
				out.failure = ev.Data.ErrorType
			}
			return false
		}
		return true
	})
	if err == nil && out.failure == "" {
		s.rememberTools(out.convID, out.tools.list(pm.Tools))
	}
	return err
}

// postmanUsage — _postman_usage из события usage.
func postmanUsage(d *PMData) map[string]any {
	if d == nil {
		return nil
	}
	return map[string]any{
		"usage":       d.Usage,
		"limit":       d.Limit,
		"usage_state": d.UsageState,
		"user_type":   d.UserType,
	}
}

// replyModel — имя модели для ответа клиенту (эхо запроса, иначе дефолт).
func (s *Server) replyModel(req *OAIRequest) string {
	if strings.TrimSpace(req.Model) != "" {
		return req.Model
	}
	return s.cfg.DefaultModel
}

// ── Non-stream ──

func (s *Server) replyNonStream(w http.ResponseWriter, ctx context.Context, req *OAIRequest, pm *ChatRequest) {
	var out collected
	if err := s.collect(ctx, pm, &out); err != nil {
		s.stats.incErr()
		s.writeUpstreamError(w, err)
		return
	}
	if out.failure != "" {
		s.stats.incErr()
		writeError(w, http.StatusBadGateway, "Postman отказал: "+out.failure)
		return
	}

	toolList := out.tools.list(pm.Tools)
	msg := map[string]any{"role": "assistant", "content": out.text.String()}
	if len(toolList) > 0 && out.text.Len() == 0 {
		msg["content"] = nil
	}
	if t := out.thinking.String(); t != "" {
		msg["reasoning_content"] = t
	}
	finish := "stop"
	if len(toolList) > 0 {
		finish = "tool_calls"
		msg["tool_calls"] = oaiToolCalls(toolList)
	}
	resp := map[string]any{
		"id":      "chatcmpl-" + newID(),
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   s.replyModel(req),
		"choices": []any{map[string]any{
			"index": 0, "message": msg, "finish_reason": finish,
		}},
		"usage": map[string]any{
			// У Postman нет токенных метрик — отдаём оценку; реальная
			// квота аккаунта лежит в _postman_usage.
			"prompt_tokens":     len(pm.Query) / 4,
			"completion_tokens": len([]rune(out.text.String())) / 4,
			"total_tokens":      (len(pm.Query) + len([]rune(out.text.String()))) / 4,
		},
	}
	if out.convID != "" {
		resp["_postman_conversation_id"] = out.convID
	}
	if pu := postmanUsage(out.usage); pu != nil {
		resp["_postman_usage"] = pu
	}
	if out.modelKey != "" {
		resp["_postman_model"] = out.modelKey
	}
	writeJSON(w, http.StatusOK, resp)
}

// ── Stream ──

func (s *Server) replyStream(w http.ResponseWriter, ctx context.Context, req *OAIRequest, pm *ChatRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	id := "chatcmpl-" + newID()
	created := time.Now().Unix()
	model := s.replyModel(req)

	send := func(v map[string]any) bool {
		v["id"] = id
		v["object"] = "chat.completion.chunk"
		v["created"] = created
		v["model"] = model
		b, err := json.Marshal(v)
		if err != nil {
			return false
		}
		if _, err := fmt.Fprintf(w, "data: %s\n\n", b); err != nil {
			return false
		}
		flusher.Flush()
		return true
	}
	chunk := func(delta map[string]any, finish any) map[string]any {
		return map[string]any{"choices": []any{map[string]any{
			"index": 0, "delta": delta, "finish_reason": finish,
		}}}
	}

	// Первый чанк с ролью — многие клиенты (Cline) ждут его до контента.
	if !send(chunk(map[string]any{"role": "assistant", "content": ""}, nil)) {
		return
	}

	var (
		usage    *PMData
		convID   string
		modelKey string
		failed   bool
		tools    = newToolCollector()
	)

	err := s.up.StreamChatAuto(ctx, pm, func(ev PMEvent) bool {
		switch ev.EventType {
		case "textChunk":
			if ev.Data.switchAgentSeparator() || ev.Data.TextContent == "" {
				return true
			}
			return send(chunk(map[string]any{"content": ev.Data.TextContent}, nil))
		case "thinkingChunk":
			t := ev.Data.ThinkingContent
			if t == "" {
				t = ev.Data.Thinking
			}
			if t == "" {
				return true
			}
			return send(chunk(map[string]any{"reasoning_content": t}, nil))
		case "conversation":
			if ev.Data.ID != "" {
				convID = ev.Data.ID
			}
			if ev.Data.ModelKey != "" {
				modelKey = ev.Data.ModelKey
			}
		case "toolCall":
			tools.merge(ev.Data, false)
		case "toolCallChunk":
			tools.merge(ev.Data, true)
		case "usage":
			d := ev.Data
			usage = &d
		case "failure":
			msg := ev.Data.UserMessage
			if msg == "" {
				msg = ev.Data.Message
			}
			if msg == "" {
				msg = ev.Data.ErrorType
			}
			failed = true
			b, _ := json.Marshal(map[string]any{"error": map[string]any{
				"message": "Postman отказал: " + msg, "type": "upstream_error",
			}})
			fmt.Fprintf(w, "data: %s\n\n", b)
			flusher.Flush()
			return false
		}
		return true
	})

	if err != nil {
		if ctx.Err() != nil {
			return
		}
		s.stats.incErr()
		msg := err.Error()
		if ae, ok := err.(*APIError); ok {
			msg = ae.Error()
		}
		b, _ := json.Marshal(map[string]any{"error": map[string]any{"message": msg, "type": "upstream_error"}})
		fmt.Fprintf(w, "data: %s\n\n", b)
		flusher.Flush()
		return
	}
	if failed {
		s.stats.incErr()
		fmt.Fprint(w, "data: [DONE]\n\n")
		flusher.Flush()
		return
	}

	toolList := tools.list(pm.Tools)
	s.rememberTools(convID, toolList)
	finish := "stop"
	if len(toolList) > 0 {
		finish = "tool_calls"
		for i, t := range toolList {
			if !send(chunk(map[string]any{"tool_calls": []any{map[string]any{
				"index": i, "id": t.ID, "type": "function",
				"function": map[string]any{"name": t.Name, "arguments": t.Arguments},
			}}}, nil)) {
				break
			}
		}
	}
	final := chunk(map[string]any{}, finish)
	if convID != "" {
		final["_postman_conversation_id"] = convID
	}
	if pu := postmanUsage(usage); pu != nil {
		final["_postman_usage"] = pu
	}
	if modelKey != "" {
		final["_postman_model"] = modelKey
	}
	send(final)
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

// writeUpstreamError транслирует ошибку апстрима клиенту по-человечески.
func (s *Server) writeUpstreamError(w http.ResponseWriter, err error) {
	ae, ok := err.(*APIError)
	if !ok {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	status := ae.Status
	msg := ae.Error()
	switch status {
	case http.StatusUnauthorized:
		msg = "токен Postman протух — войди заново в Postman Desktop (перезапусти мост, он подхватит свежую сессию) или обнови POSTMAN_ACCESS_TOKEN"
	case http.StatusForbidden:
		msg = "Postman отклонил запрос (403): проверь AI в настройках аккаунта (https://postman.co/settings/team/ai); " +
			"при TOOL_VALIDATION_ERROR — задай свежие POSTMAN_TOOLS_HASH/POSTMAN_TERMS_HASH. " + ae.Error()
	case http.StatusTooManyRequests:
		msg = "квота Postman исчерпана: " + ae.Error()
	}
	writeError(w, status, msg)
}
