// anthropic.go — /v1/messages в формате Anthropic Messages API (Claude Code).
//
// Апстрим тот же (gateway.postman.com), отличается только формат: content-блоки
// вместо message.delta и SSE-события message_start / content_block_delta /
// message_delta / message_stop. System уходит в Postman целиком через
// backgroundContext.AGENTS_MD.
//
// Tools: определения уходят в clientTools.thirdParty, вызовы возвращаются
// tool_use блоками (stop_reason tool_use), результаты клиента (tool_result)
// превращаются в TOOL_RESPONSE того же диалога.
package app

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
)

type AntMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"` // string | []block
}

type AntTool struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	InputSchema json.RawMessage `json:"input_schema,omitempty"`
}

type AntRequest struct {
	Model       string       `json:"model"`
	Messages    []AntMessage `json:"messages"`
	System      any          `json:"system,omitempty"`
	Stream      bool         `json:"stream"`
	Tools       []AntTool    `json:"tools"` // → clientTools.thirdParty
	MaxTokens   int          `json:"max_tokens"`
	Temperature *float64     `json:"temperature"`
	TopP        *float64     `json:"top_p"`
	StopSeqs    []string     `json:"stop_sequences"`
	Thinking    any          `json:"thinking,omitempty"`

	PostmanConversationID string `json:"_postman_conversation_id"`
	ConversationID        string `json:"conversation_id"`
}

func (r *AntRequest) convID() string {
	if r.PostmanConversationID != "" {
		return r.PostmanConversationID
	}
	return r.ConversationID
}

func (s *Server) antReplyModel(req *AntRequest) string {
	if strings.TrimSpace(req.Model) != "" {
		return req.Model
	}
	return s.cfg.DefaultModel
}

func (s *Server) handleMessages(w http.ResponseWriter, r *http.Request) {
	s.stats.incReq()

	var req AntRequest
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

	pm, err := s.toPostmanAnt(&req)
	if err != nil {
		s.stats.incErr()
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if !req.Stream {
		s.replyAntNonStream(w, r.Context(), pm, &req)
		return
	}
	s.replyAntStream(w, r.Context(), pm, &req)
}

// toPostmanAnt собирает ChatRequest: system → AGENTS_MD, история текстом → query.
// Замыкающий блок tool_result превращается в TOOL_RESPONSE того же диалога.
func (s *Server) toPostmanAnt(req *AntRequest) (*ChatRequest, error) {
	sys := antSystem(req.System)
	pm := &ChatRequest{
		Model:          s.resolveModel(req.Model),
		SystemContext:  sys,
		ConversationID: req.convID(),
		Thinking:       true,
		Tools:          parseAntTools(req.Tools),
	}

	results := antToolResults(req.Messages)
	if len(results) > 0 {
		callIDs := make([]string, 0, len(results))
		for _, r := range results {
			callIDs = append(callIDs, r.CallID)
		}
		convID, groupID, err := s.lookupContinuation(callIDs, req.convID())
		if err != nil {
			// мост не помнит вызовы (перезапуск) — не роняем запрос клиента,
			// продолжаем свежим диалогом, вшив результаты в query.
			log.Printf("[postman2api] %v — продолжаю свежим диалогом", err)
			pm.ConversationID = ""
			pm.Query = foldToolResults(results, "", s.cfg.MaxQuery)
			return pm, nil
		}
		pm.ConversationID = convID
		pm.ToolResults = results
		pm.ToolGroupID = groupID
		return pm, nil
	}

	turns := make([]ChatTurn, 0, len(req.Messages))
	for _, m := range req.Messages {
		if text := antText(m.Content); text != "" {
			turns = append(turns, ChatTurn{Role: m.Role, Text: text})
		}
	}
	pm.Query = buildQuery(turns, sys, s.cfg.MaxQuery)
	return pm, nil
}

// parseAntTools — определения инструментов из Anthropic-запроса.
func parseAntTools(tools []AntTool) []ToolDef {
	out := make([]ToolDef, 0, len(tools))
	for _, t := range tools {
		if t.Name == "" {
			continue
		}
		var params map[string]any
		if len(t.InputSchema) > 0 {
			_ = json.Unmarshal(t.InputSchema, &params)
		}
		out = append(out, ToolDef{Name: t.Name, Description: t.Description, Parameters: params})
	}
	return out
}

// antToolResults — tool_result блоки замыкающих user-сообщений истории.
func antToolResults(msgs []AntMessage) []ToolResult {
	var results []ToolResult
	for i := len(msgs) - 1; i >= 0; i-- {
		if strings.ToLower(msgs[i].Role) != "user" {
			break
		}
		blockResults := parseToolResultBlocks(msgs[i].Content)
		if len(blockResults) == 0 {
			break
		}
		results = append(blockResults, results...)
	}
	return results
}

func parseToolResultBlocks(raw json.RawMessage) []ToolResult {
	var blocks []struct {
		Type      string          `json:"type"`
		ToolUseID string          `json:"tool_use_id"`
		IsError   bool            `json:"is_error"`
		Content   json.RawMessage `json:"content"`
	}
	if len(raw) == 0 || json.Unmarshal(raw, &blocks) != nil {
		return nil
	}
	var out []ToolResult
	for _, b := range blocks {
		if b.Type != "tool_result" || b.ToolUseID == "" {
			continue
		}
		status := "SUCCESS"
		if b.IsError {
			status = "FAILED"
		}
		out = append(out, ToolResult{CallID: b.ToolUseID, Content: antText(b.Content), Status: status})
	}
	return out
}

// antText — текст Anthropic-контента: строка или text-блоки.
func antText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		var b strings.Builder
		for _, blk := range blocks {
			if blk.Type == "text" && blk.Text != "" {
				if b.Len() > 0 {
					b.WriteString("\n\n")
				}
				b.WriteString(blk.Text)
			}
		}
		return b.String()
	}
	return ""
}

// antSystem — system может быть строкой или массивом {text}.
func antSystem(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case []any:
		var sb strings.Builder
		for _, item := range t {
			if m, ok := item.(map[string]any); ok {
				if text := str(m["text"]); text != "" {
					if sb.Len() > 0 {
						sb.WriteString("\n\n")
					}
					sb.WriteString(text)
				}
			}
		}
		return sb.String()
	}
	return ""
}

// ── Non-stream ──

func (s *Server) replyAntNonStream(w http.ResponseWriter, ctx context.Context, pm *ChatRequest, req *AntRequest) {
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
	blocks := []any{}
	if t := out.thinking.String(); t != "" {
		blocks = append(blocks, map[string]any{
			"type": "thinking", "thinking": t, "signature": "",
		})
	}
	blocks = append(blocks, map[string]any{"type": "text", "text": out.text.String()})
	stopReason := "end_turn"
	if len(toolList) > 0 {
		stopReason = "tool_use"
		for _, t := range toolList {
			input := map[string]any{}
			_ = json.Unmarshal([]byte(t.Arguments), &input)
			blocks = append(blocks, map[string]any{
				"type": "tool_use", "id": t.ID, "name": t.Name, "input": input,
			})
		}
	}

	resp := map[string]any{
		"id":            "msg_" + newID(),
		"type":          "message",
		"role":          "assistant",
		"model":         s.antReplyModel(req),
		"content":       blocks,
		"stop_reason":   stopReason,
		"stop_sequence": nil,
		"usage": map[string]any{
			"input_tokens":  len(pm.Query) / 4,
			"output_tokens": len([]rune(out.text.String())) / 4,
		},
	}
	if out.convID != "" {
		resp["_postman_conversation_id"] = out.convID
	}
	if pu := postmanUsage(out.usage); pu != nil {
		resp["_postman_usage"] = pu
	}
	writeJSON(w, http.StatusOK, resp)
}

// ── Stream ──

func (s *Server) replyAntStream(w http.ResponseWriter, ctx context.Context, pm *ChatRequest, req *AntRequest) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeError(w, http.StatusInternalServerError, "streaming unsupported")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.WriteHeader(http.StatusOK)

	msgID := "msg_" + newID()
	model := s.antReplyModel(req)

	send := func(event string, v any) bool {
		b, err := json.Marshal(v)
		if err != nil {
			return false
		}
		if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, b); err != nil {
			return false
		}
		flusher.Flush()
		return true
	}

	// message_start с пустым content — Claude Code отсчитывает от него состояние.
	if !send("message_start", map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id": msgID, "type": "message", "role": "assistant", "model": model,
			"content": []any{}, "stop_reason": nil, "stop_sequence": nil,
			"usage": map[string]any{"input_tokens": 0, "output_tokens": 0},
		},
	}) {
		return
	}

	var (
		thinkIdx = -1
		textIdx  = -1
		nextIdx  = 0
		closed   = map[int]bool{}
		outRunes = 0
		failed   bool
		convID   string
		tools    = newToolCollector()
	)

	closeBlock := func(idx int) {
		if idx < 0 || closed[idx] {
			return
		}
		closed[idx] = true
		send("content_block_stop", map[string]any{"type": "content_block_stop", "index": idx})
	}

	err := s.up.StreamChatAuto(ctx, pm, func(ev PMEvent) bool {
		switch ev.EventType {
		case "thinkingChunk":
			t := ev.Data.ThinkingContent
			if t == "" {
				t = ev.Data.Thinking
			}
			if t == "" {
				return true
			}
			if thinkIdx < 0 {
				if textIdx >= 0 {
					closeBlock(textIdx)
					textIdx = -1
				}
				thinkIdx = nextIdx
				nextIdx++
				if !send("content_block_start", map[string]any{
					"type": "content_block_start", "index": thinkIdx,
					"content_block": map[string]any{"type": "thinking", "thinking": ""},
				}) {
					return false
				}
			}
			return send("content_block_delta", map[string]any{
				"type": "content_block_delta", "index": thinkIdx,
				"delta": map[string]any{"type": "thinking_delta", "thinking": t},
			})

		case "textChunk":
			if ev.Data.switchAgentSeparator() || ev.Data.TextContent == "" {
				return true
			}
			if thinkIdx >= 0 {
				closeBlock(thinkIdx)
				thinkIdx = -1
			}
			if textIdx < 0 {
				textIdx = nextIdx
				nextIdx++
				if !send("content_block_start", map[string]any{
					"type": "content_block_start", "index": textIdx,
					"content_block": map[string]any{"type": "text", "text": ""},
				}) {
					return false
				}
			}
			outRunes += len([]rune(ev.Data.TextContent))
			return send("content_block_delta", map[string]any{
				"type": "content_block_delta", "index": textIdx,
				"delta": map[string]any{"type": "text_delta", "text": ev.Data.TextContent},
			})

		case "conversation":
			if ev.Data.ID != "" {
				convID = ev.Data.ID
			}
		case "toolCall":
			tools.merge(ev.Data, false)
		case "toolCallChunk":
			tools.merge(ev.Data, true)
		case "failure":
			msg := ev.Data.UserMessage
			if msg == "" {
				msg = ev.Data.Message
			}
			if msg == "" {
				msg = ev.Data.ErrorType
			}
			failed = true
			send("error", map[string]any{
				"type":  "error",
				"error": map[string]any{"type": "api_error", "message": "Postman отказал: " + msg},
			})
			return false
		}
		return true
	})

	if thinkIdx >= 0 {
		closeBlock(thinkIdx)
	}
	if textIdx >= 0 {
		closeBlock(textIdx)
	}

	if failed {
		s.stats.incErr()
		return
	}
	if err != nil {
		if ctx.Err() == nil {
			s.stats.incErr()
			send("error", map[string]any{
				"type":  "error",
				"error": map[string]any{"type": "api_error", "message": err.Error()},
			})
		}
		return
	}

	// Tool-вызовы идут после текста: блок tool_use, input одним
	// input_json_delta (апстрим присылает готовые аргументы) и закрытие.
	toolList := tools.list(pm.Tools)
	s.rememberTools(convID, toolList)
	stopReason := "end_turn"
	if len(toolList) > 0 {
		stopReason = "tool_use"
		idx := nextIdx
		for _, t := range toolList {
			send("content_block_start", map[string]any{
				"type": "content_block_start", "index": idx,
				"content_block": map[string]any{
					"type": "tool_use", "id": t.ID, "name": t.Name, "input": map[string]any{},
				},
			})
			send("content_block_delta", map[string]any{
				"type": "content_block_delta", "index": idx,
				"delta": map[string]any{"type": "input_json_delta", "partial_json": t.Arguments},
			})
			send("content_block_stop", map[string]any{"type": "content_block_stop", "index": idx})
			idx++
		}
	}

	send("message_delta", map[string]any{
		"type":  "message_delta",
		"delta": map[string]any{"stop_reason": stopReason, "stop_sequence": nil},
		"usage": map[string]any{"output_tokens": outRunes / 4},
	})
	send("message_stop", map[string]any{"type": "message_stop"})
}
