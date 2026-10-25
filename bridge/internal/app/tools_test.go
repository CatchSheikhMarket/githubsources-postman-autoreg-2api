package app

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestEncodeDecodeToolName(t *testing.T) {
	enc := encodeToolName("get_weather")
	if enc != "client__get_weather" {
		t.Fatalf("encode = %q", enc)
	}
	names := ToolNameMap([]ToolDef{{Name: "get_weather"}})
	name, args := decodeToolName(enc, `{"city":"Париж"}`, names)
	if name != "get_weather" || args != `{"city":"Париж"}` {
		t.Fatalf("decode = %q %q", name, args)
	}

	// без карты объявлений префикс всё равно снимается
	if name, _ := decodeToolName("client__read_file", `{}`, nil); name != "read_file" {
		t.Fatalf("prefix strip = %q", name)
	}

	// обёртка executeNamespaceTool разворачивается во вложенный вызов
	wrapped := `{"namespace":"external-client","toolName":"client__get_weather","input":{"city":"Токио"}}`
	name, args = decodeToolName("executeNamespaceTool", wrapped, nil)
	if name != "get_weather" || !strings.Contains(args, "Токио") {
		t.Fatalf("unwrap = %q %q", name, args)
	}

	// длинное имя не выходит за лимит 64 символа
	if got := encodeToolName(strings.Repeat("very_long_name_", 6)); len(got) > 64 {
		t.Fatalf("encoded name too long: %d", len(got))
	}
}

func TestClosingToolResults(t *testing.T) {
	msgs := []OAIMessage{
		{Role: "user", Content: json.RawMessage(`"привет"`)},
		{Role: "assistant", Content: json.RawMessage(`null`), ToolCalls: []OAIToolCall{{ID: "call_1"}}},
		{Role: "tool", ToolCallID: "call_1", Content: json.RawMessage(`"result 1"`)},
		{Role: "tool", ToolCallID: "call_2", Content: json.RawMessage(`"result 2"`)},
	}
	res := closingToolResults(msgs)
	if len(res) != 2 || res[0].CallID != "call_1" || res[1].CallID != "call_2" || res[0].Content != "result 1" {
		t.Fatalf("closing = %+v", res)
	}

	// tool-сообщение в середине истории не является замыкающим блоком
	msgs2 := []OAIMessage{
		{Role: "tool", ToolCallID: "call_old", Content: json.RawMessage(`"old"`)},
		{Role: "user", Content: json.RawMessage(`"дальше"`)},
	}
	if got := closingToolResults(msgs2); len(got) != 0 {
		t.Fatalf("closing should be empty, got %+v", got)
	}
}

func TestBuildBodyToolResponse(t *testing.T) {
	cfg := &Config{
		Platform: "DESKTOP_LINUX", WorkspaceID: "ws",
		Product: "workspace_localmode_v12", ProductTools: "workspace_tools_sdk_localmode",
		ToolsHash: "h1", TermsHash: "t1", ToolsHashSDK: "h2", TermsHashSDK: "t2",
	}
	u := &Upstream{cfg: cfg}

	// одиночный результат (без группы) → toolCallId/toolResponse
	body := u.buildBody(&ChatRequest{
		Model: "GPT_56_SOL", ConversationID: "conv-1",
		ToolResults: []ToolResult{{CallID: "call_1", Content: "ok"}},
	})
	input := body["input"].(map[string]any)
	if input["chatType"] != "TOOL_RESPONSE" || input["toolCallId"] != "call_1" || input["query"] != "" {
		t.Fatalf("input = %#v", input)
	}
	if _, has := input["startedFrom"]; has {
		t.Fatal("TOOL_RESPONSE must not carry startedFrom")
	}
	if ct := body["clientTools"].(map[string]any); ct["nativeToolsHash"] != "h2" {
		t.Fatalf("hash = %v", ct["nativeToolsHash"])
	}

	// группа результатов → toolResponses массивом
	body = u.buildBody(&ChatRequest{
		Model: "GPT_56_SOL", ConversationID: "conv-2", ToolGroupID: "grp-1",
		ToolResults: []ToolResult{
			{CallID: "call_1", Content: "a"},
			{CallID: "call_2", Content: "b", Status: "FAILED"},
		},
	})
	input = body["input"].(map[string]any)
	rs, ok := input["toolResponses"].([]any)
	if !ok || len(rs) != 2 {
		t.Fatalf("toolResponses = %#v", input["toolResponses"])
	}
	if second := rs[1].(map[string]any); second["toolResponseStatus"] != "FAILED" {
		t.Fatalf("status = %v", second["toolResponseStatus"])
	}
	if input["toolCallGroupId"] != "grp-1" {
		t.Fatalf("group = %v", input["toolCallGroupId"])
	}

	// определения инструментов уходят закодированными в thirdParty
	body = u.buildBody(&ChatRequest{Model: "GPT_56_SOL", Query: "hi", Tools: []ToolDef{{Name: "get_weather"}}})
	ct := body["clientTools"].(map[string]any)
	defs := ct["thirdParty"].(map[string]any)["external-client"].(map[string]any)["tools"].([]any)
	if name := defs[0].(map[string]any)["name"]; name != "client__get_weather" {
		t.Fatalf("tool name = %v", name)
	}

	// без инструментов — старый product и хэши
	body = u.buildBody(&ChatRequest{Model: "GPT_56_SOL", Query: "hi"})
	if ct := body["clientTools"].(map[string]any); ct["nativeToolsHash"] != "h1" {
		t.Fatalf("plain hash = %v", ct["nativeToolsHash"])
	}
}

func TestToolCollector(t *testing.T) {
	toData := func(v map[string]any) PMData {
		raw, _ := json.Marshal(v)
		var d PMData
		if err := json.Unmarshal(raw, &d); err != nil {
			t.Fatal(err)
		}
		return d
	}

	tc := newToolCollector()
	tc.merge(toData(map[string]any{"toolCalls": []any{map[string]any{
		"id": "call_1", "toolCallGroupId": "grp",
		"function": map[string]any{"name": "client__get_weather", "arguments": `{"city":`},
	}}}), false)
	// дельта аргументов приходит отдельным чанком
	tc.merge(toData(map[string]any{"toolCalls": []any{map[string]any{
		"id":       "call_1",
		"function": map[string]any{"arguments": `"Токио"}`},
	}}}), true)

	got := tc.list([]ToolDef{{Name: "get_weather"}})
	if len(got) != 1 {
		t.Fatalf("list = %+v", got)
	}
	if got[0].Name != "get_weather" || got[0].Arguments != `{"city":"Токио"}` || got[0].GroupID != "grp" {
		t.Fatalf("tool = %+v", got[0])
	}
}

func TestAntToolResults(t *testing.T) {
	msgs := []AntMessage{
		{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"вопрос"}]`)},
		{Role: "assistant", Content: json.RawMessage(`[{"type":"tool_use","id":"toolu_1","name":"get_price","input":{"asset":"BTC"}}]`)},
		{Role: "user", Content: json.RawMessage(`[{"type":"tool_result","tool_use_id":"toolu_1","content":"BTC = 97500"}]`)},
	}
	res := antToolResults(msgs)
	if len(res) != 1 || res[0].CallID != "toolu_1" || res[0].Status != "SUCCESS" || res[0].Content != "BTC = 97500" {
		t.Fatalf("antToolResults = %+v", res)
	}
}

func TestLookupContinuation(t *testing.T) {
	s := &Server{tools: newToolRegistry()}
	s.tools.put("call_1", "conv-1", "grp", "get_weather")
	s.tools.put("call_2", "conv-1", "grp", "get_price")

	conv, grp, err := s.lookupContinuation([]string{"call_1", "call_2"}, "")
	if err != nil || conv != "conv-1" || grp != "grp" {
		t.Fatalf("lookup = %q %q %v", conv, grp, err)
	}

	// fallback из самого запроса
	conv, _, err = s.lookupContinuation([]string{"unknown"}, "conv-x")
	if err != nil || conv != "conv-x" {
		t.Fatalf("fallback = %q %v", conv, err)
	}

	// ничего не известно — ошибка
	if _, _, err := s.lookupContinuation([]string{"unknown"}, ""); err == nil {
		t.Fatal("expected error for unknown tool call")
	}
}

func TestForeignToolFiltered(t *testing.T) {
	names := ToolNameMap([]ToolDef{{Name: "get_weather"}})

	// нативный вызов апстрима (listDirectory без префикса client__) — чужой
	if isClientToolEncoded("listDirectory", names) {
		t.Fatal("native tool must not classify as client")
	}
	// объявленный и закодированный вызовы — клиентские
	if !isClientToolEncoded("client__get_weather", names) {
		t.Fatal("encoded client tool must classify as client")
	}
	if !isClientToolEncoded("executeNamespaceTool", nil) {
		t.Fatal("namespace wrapper must classify as client")
	}

	// коллектор не отдаёт чужие вызовы наружу
	toData := func(v map[string]any) PMData {
		raw, _ := json.Marshal(v)
		var d PMData
		if err := json.Unmarshal(raw, &d); err != nil {
			t.Fatal(err)
		}
		return d
	}
	tc := newToolCollector()
	tc.merge(toData(map[string]any{"toolCalls": []any{map[string]any{
		"id": "native_1", "name": "listDirectory", "arguments": `{}`,
	}}}), false)
	tc.merge(toData(map[string]any{"toolCalls": []any{map[string]any{
		"id": "client_1", "name": "client__get_weather", "arguments": `{"city":"Paris"}`,
	}}}), false)
	got := tc.list([]ToolDef{{Name: "get_weather"}})
	if len(got) != 1 || got[0].Name != "get_weather" {
		t.Fatalf("list = %+v", got)
	}
}

func TestIsChatLost(t *testing.T) {
	if !isChatLost(PMData{UserMessage: "Looks like I couldn't find this chat. Try creating a new one."}) {
		t.Fatal("chat-lost not detected")
	}
	if isChatLost(PMData{Message: "This message got flagged by our safety checks"}) {
		t.Fatal("false positive on unrelated failure")
	}
}

func TestFoldToolResults(t *testing.T) {
	q := foldToolResults([]ToolResult{
		{CallID: "call_1", Content: "18C"},
		{CallID: "call_2", Status: "FAILED", Content: ""},
	}, "question", 9800)
	for _, want := range []string{"call_1", "18C", "FAILED", "question"} {
		if !strings.Contains(q, want) {
			t.Fatalf("fold missing %q: %q", want, q)
		}
	}
	if q := foldToolResults(nil, "", 100); len(q) > 100+len("…") {
		t.Fatalf("fold not capped: %d", len(q))
	}
}

func TestTranslateNativeTool(t *testing.T) {
	client := newClientToolSet([]ToolDef{{Name: "Bash"}, {Name: "Read"}, {Name: "Write"}, {Name: "Grep"}})

	// listDirectory → Bash(find) с глубиной и игнор-глобами
	name, args := translateNativeCall("listDirectory", map[string]any{
		"directoryPath": "", "depth": "3",
		"ignoreGlobs": []any{".git", "node_modules"},
	}, client)
	if name != "Bash" || args["command"] != `find '.' -mindepth 1 -maxdepth 3 ! -path './.git' ! -path './.git/*' ! -path './node_modules' ! -path './node_modules/*' -print | sort` {
		t.Fatalf("listDirectory = %q %#v", name, args)
	}

	// readFile → Read
	name, args = translateNativeCall("readFile", map[string]any{
		"filePath": "/tmp/x.go", "offset": float64(10), "limit": float64(5),
	}, client)
	if name != "Read" || args["file_path"] != "/tmp/x.go" || args["offset"] != float64(10) {
		t.Fatalf("readFile = %q %#v", name, args)
	}

	// createFile → Write
	name, args = translateNativeCall("createFile", map[string]any{
		"path": "a.txt", "contents": "hi",
	}, client)
	if name != "Write" || args["file_path"] != "a.txt" || args["content"] != "hi" {
		t.Fatalf("createFile = %q %#v", name, args)
	}

	// executeBashCommand → Bash
	name, args = translateNativeCall("executeBashCommand", map[string]any{
		"command": "ls -la", "description": "list",
	}, client)
	if name != "Bash" || args["command"] != "ls -la" || args["description"] != "list" {
		t.Fatalf("executeBashCommand = %q %#v", name, args)
	}

	// searchInFiles → Grep с экранированным паттерном
	name, args = translateNativeCall("searchInFiles", map[string]any{
		"query": "foo.bar", "directoryPath": "/tmp", "maxResults": float64(50),
	}, client)
	if name != "Grep" || args["pattern"] != `foo\.bar` || args["path"] != "/tmp" ||
		args["output_mode"] != "content" || args["head_limit"] != 50 {
		t.Fatalf("searchInFiles = %q %#v", name, args)
	}

	// requirements
	if needs, ok := nativeToolNeeds("listDirectory"); !ok || needs[0] != "Bash" {
		t.Fatalf("needs = %v %v", needs, ok)
	}
	if _, ok := nativeToolNeeds("unknownNativeThing"); ok {
		t.Fatal("unknown native must not be translatable")
	}

	// коллектор отдаёт нативный вызов переведённым
	toData := func(v map[string]any) PMData {
		raw, _ := json.Marshal(v)
		var d PMData
		_ = json.Unmarshal(raw, &d)
		return d
	}
	tc := newToolCollector()
	tc.merge(toData(map[string]any{"toolCalls": []any{map[string]any{
		"id": "native_1", "name": "listDirectory", "arguments": `{"directoryPath":"","depth":3,"ignoreGlobs":[".git"]}`,
	}}}), false)
	got := tc.list([]ToolDef{{Name: "Bash"}})
	if len(got) != 1 || got[0].Name != "Bash" {
		t.Fatalf("translated list = %+v", got)
	}
	var in map[string]any
	if err := json.Unmarshal([]byte(got[0].Arguments), &in); err != nil || !strings.Contains(in["command"].(string), "find") {
		t.Fatalf("translated args = %s %v", got[0].Arguments, err)
	}
}
