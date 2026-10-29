package admission

import (
	"kinich-go/internal/domain"
	"sync"
)

type permit struct {
	worker, key, tenant, token string
	grant                      func(string)
}

// Gate reparte forwards en vuelo por tenant; un permiso sólo se libera al terminar o comprobar salida del worker.
type Gate struct {
	mu         sync.Mutex
	Limit      int
	waiting    []permit
	active     map[string]permit
	lastTenant string
}

func NewGate(limit int) *Gate { return &Gate{Limit: limit, active: map[string]permit{}} }
func (g *Gate) Request(worker, tenant, key string, grant func(string)) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	for _, p := range g.waiting {
		if p.worker == worker && p.key == key {
			return false
		}
	}
	for _, p := range g.active {
		if p.worker == worker && p.key == key {
			return false
		}
	}
	if len(g.waiting) >= 2048 {
		return false
	}
	g.waiting = append(g.waiting, permit{worker: worker, tenant: tenant, key: key, grant: grant})
	g.dispatch()
	return true
}
func (g *Gate) dispatch() {
	for len(g.active) < g.Limit && len(g.waiting) > 0 {
		i := 0
		for j, p := range g.waiting {
			if p.tenant != g.lastTenant {
				i = j
				break
			}
		}
		p := g.waiting[i]
		g.waiting = append(g.waiting[:i], g.waiting[i+1:]...)
		p.token = domain.ID()
		g.active[p.token] = p
		g.lastTenant = p.tenant
		go p.grant(p.token)
	}
}
func (g *Gate) Done(worker, token string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if p, ok := g.active[token]; ok && p.worker == worker {
		delete(g.active, token)
		g.dispatch()
	}
}
func (g *Gate) WorkerExited(worker string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	for token, p := range g.active {
		if p.worker == worker {
			delete(g.active, token)
		}
	}
	out := g.waiting[:0]
	for _, p := range g.waiting {
		if p.worker != worker {
			out = append(out, p)
		}
	}
	g.waiting = out
	g.dispatch()
}
func (g *Gate) Count() (int, int) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.active), len(g.waiting)
}
