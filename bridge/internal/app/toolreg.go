// toolreg.go — память о выданных Postman tool-вызовах.
//
// Клиент (Cline, Claude Code и др.) возвращает результаты инструментов
// следующим запросом, но Postman принимает TOOL_RESPONSE только с
// conversationId того диалога, где вызовы были выданы. Мост держит
// соответствие toolCallId → conversationId/groupId в памяти процесса.
package app

import (
	"sync"
	"time"
)

type toolRecord struct {
	conversationID string
	groupID        string
	name           string
	ts             time.Time
}

type toolRegistry struct {
	mu    sync.Mutex
	calls map[string]toolRecord
	order []string
}

const (
	toolRecordTTL = 3 * time.Hour
	toolRecordMax = 4000
)

func newToolRegistry() *toolRegistry {
	return &toolRegistry{calls: map[string]toolRecord{}}
}

func (r *toolRegistry) put(callID, conversationID, groupID, name string) {
	if callID == "" {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, ok := r.calls[callID]; !ok {
		r.order = append(r.order, callID)
	}
	r.calls[callID] = toolRecord{
		conversationID: conversationID,
		groupID:        groupID,
		name:           name,
		ts:             time.Now(),
	}
	r.gcLocked()
}

func (r *toolRegistry) get(callID string) (toolRecord, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	rec, ok := r.calls[callID]
	if !ok || time.Since(rec.ts) > toolRecordTTL {
		return toolRecord{}, false
	}
	return rec, true
}

func (r *toolRegistry) size() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.calls)
}

// gcLocked — уборка по пределу размера и TTL (вызывается под mu).
func (r *toolRegistry) gcLocked() {
	for len(r.order) > toolRecordMax {
		old := r.order[0]
		r.order = r.order[1:]
		delete(r.calls, old)
	}
	cutoff := time.Now().Add(-toolRecordTTL)
	for len(r.order) > 0 {
		old := r.order[0]
		rec, ok := r.calls[old]
		if ok && rec.ts.After(cutoff) {
			break
		}
		delete(r.calls, old)
		r.order = r.order[1:]
	}
}
