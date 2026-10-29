package reconcile

import (
	"context"
	"database/sql"
	"errors"
	"slices"
	"sync"
	"time"

	"kinich-go/internal/admission"
	"kinich-go/internal/catalog"
	"kinich-go/internal/domain"
	"kinich-go/internal/store"
	"kinich-go/internal/supervisor"
)

type Reconciler struct {
	Gate       *admission.Gate
	Store      *store.Store
	Catalog    *catalog.Catalog
	Manager    *supervisor.Manager
	Budget     admission.Budget
	Event      func(context.Context, domain.Execution, domain.Event) error
	mu         sync.Mutex
	lastTenant string
	stopping   bool
}

func (r *Reconciler) Run(ctx context.Context) {
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = r.Tick(ctx)
		}
	}
}
func (r *Reconciler) Tick(ctx context.Context) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	es, err := r.Store.Executions(ctx, true)
	if err != nil {
		return err
	}
	tenants := []string{}
	for _, e := range es {
		if e.State == "queued" && !slices.Contains(tenants, e.Owner.Key()) {
			tenants = append(tenants, e.Owner.Key())
		}
	}
	if i := slices.Index(tenants, r.lastTenant); i >= 0 && len(tenants) > 1 {
		tenants = append(tenants[i+1:], tenants[:i+1]...)
	}
	for _, e := range es {
		if e.State == "queued" {
			continue
		}
		p := r.Manager.Get(e.ID)
		if p != nil && p.Exited() {
			if r.Gate != nil {
				r.Gate.WorkerExited(p.ID)
			}
			if err = r.Store.Finish(ctx, e); err != nil {
				return err
			}
			r.Manager.Remove(e.ID)
			continue
		}
		if p == nil {
			var pid int
			err := r.Store.DB.QueryRowContext(ctx, "SELECT pid FROM workers WHERE id=?", e.WorkerID).Scan(&pid)
			if errors.Is(err, sql.ErrNoRows) || (err == nil && supervisor.GroupGone(pid)) {
				e.Terminal = "failed"
				e.Reason = "worker_not_present"
				if err = r.Store.Finish(ctx, e); err != nil {
					return err
				}
			}
			continue
		}
		if e.Desired == "stop" || e.State == "stopping" {
			p.Stop()
		}
	}
	if err = r.settle(ctx); err != nil {
		return err
	}
	if r.stopping {
		return nil
	}
	for _, tenant := range tenants {
		for _, e := range es {
			if e.State != "queued" || e.Owner.Key() != tenant {
				continue
			}
			if store.Expired(e.Deadline) {
				if err = r.retire(ctx, e, "expired", "queue_deadline"); err != nil {
					return err
				}
				continue
			}
			c, err := r.Store.Camera(ctx, e.Owner, e.CameraID)
			if err != nil {
				return err
			}
			if !c.Enabled {
				if err = r.retire(ctx, e, "cancelled", "camera_disabled"); err != nil {
					return err
				}
				continue
			}
			err = r.prepare(ctx, &e)
			if err != nil {
				var f *domain.Fault
				if errors.As(err, &f) && f.Code == "capacity_wait" {
					break
				}
				_ = r.retire(ctx, e, "failed", "admission_failed")
				break
			}
			r.lastTenant = tenant
			p, err := r.Manager.Start(ctx, e)
			if err != nil {
				_ = r.retire(ctx, e, "failed", "worker_start_failed")
				break
			}
			_, err = r.Store.DB.ExecContext(ctx, "UPDATE workers SET pid=?,state='alive' WHERE id=?", p.Cmd.Process.Pid, p.ID)
			if err != nil {
				p.Kill()
				return err
			}
			source, err := r.Store.Source(ctx, e.Owner, e.CameraID)
			if e.Kind != "stream" {
				var encrypted []byte
				err = r.Store.DB.QueryRowContext(ctx, "SELECT media FROM execution_media WHERE environment=? AND tenant_id=? AND execution_id=?", e.Environment, e.TenantID, e.ID).Scan(&encrypted)
				if err == nil {
					e.Media, err = r.Store.Unseal(e.Owner, encrypted)
				}
				source = ""
			}
			if err != nil {
				p.Kill()
				return err
			}
			go r.observe(ctx, e, p)
			if err = p.Send(supervisor.Message{Kind: "open", CameraID: e.CameraID, Members: e.Members, Revision: e.Revision, Source: source, Media: e.Media, Mode: e.Kind, LeaseMS: supervisor.LeaseMS}); err != nil {
				_ = r.retire(ctx, e, "failed", "worker_open_failed")
				p.Stop()
			}
			break
		}
	}
	return nil
}
func (r *Reconciler) prepare(ctx context.Context, e *domain.Execution) error {
	return r.Store.Tx(ctx, func(tx *sql.Tx) error {
		current, err := store.Execution(ctx, tx, e.Owner, e.ID)
		if err != nil {
			return err
		}
		if current.State != "queued" || current.Desired != "run" {
			return domain.Conflict
		}
		c, err := store.Camera(ctx, tx, e.Owner, e.CameraID)
		if err != nil {
			return err
		}
		if !c.Enabled {
			return domain.Conflict
		}
		if err = r.Budget.Reserve(ctx, tx, current, r.Catalog.Cost(current.Members)); err != nil {
			return err
		}
		current.State = "preparing"
		current.WorkerID = domain.ID()
		for i := range current.Members {
			current.Members[i].State = "warming"
		}
		if err = store.SaveExecution(tx, current); err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, "INSERT INTO workers VALUES(?,?,?,?,?,?,?)", current.WorkerID, current.Environment, current.TenantID, current.ID, r.Manager.ControllerID, 0, "starting")
		*e = current
		return err
	})
}
func (r *Reconciler) retire(ctx context.Context, e domain.Execution, terminal, reason string) error {
	return r.Store.Tx(ctx, func(tx *sql.Tx) error {
		current, err := store.Execution(ctx, tx, e.Owner, e.ID)
		if err != nil {
			return err
		}
		if err = store.Retire(tx, &current, terminal, reason); err != nil {
			return err
		}
		if !current.Active() {
			state := "succeeded"
			if terminal == "failed" {
				state = "failed"
			}
			return store.UpdateOperation(tx, e.Owner, current.OperationID, state, current)
		}
		return nil
	})
}
func (r *Reconciler) observe(ctx context.Context, e domain.Execution, p *supervisor.Process) {
	renew := time.NewTicker(1500 * time.Millisecond)
	defer renew.Stop()
	last := time.Now()
	ready := false
	for {
		select {
		case <-ctx.Done():
			p.Stop()
			return
		case <-p.Done:
			return
		case <-renew.C:
			current, err := r.Store.Execution(ctx, e.Owner, e.ID)
			if err != nil || current.Desired != "run" || time.Since(last) > 7*time.Second {
				if err == nil && current.Desired == "run" {
					_ = r.retire(ctx, e, "failed", "worker_lease_unconfirmed")
				}
				p.Stop()
				return
			}
			if err = p.Send(supervisor.Message{Kind: "renew", LeaseMS: supervisor.LeaseMS}); err != nil {
				_ = r.retire(ctx, e, "failed", "lease_renewal_failed")
				p.Stop()
				return
			}
		case msg, ok := <-p.Messages:
			if !ok {
				return
			}
			last = time.Now()
			switch msg.Kind {
			case "inference_request":
				current, err := r.Store.Execution(ctx, e.Owner, e.ID)
				valid := err == nil && current.Desired == "run" && current.Generation == e.Generation
				found := false
				for _, member := range current.Members {
					if member.ID == msg.MemberID && member.Generation == msg.MemberGeneration {
						found = true
					}
				}
				if !valid || !found || r.Gate == nil {
					_ = p.Send(supervisor.Message{Kind: "permit", MemberID: msg.MemberID, MemberGeneration: msg.MemberGeneration})
					continue
				}
				memberID, memberGeneration := msg.MemberID, msg.MemberGeneration
				if !r.Gate.Request(p.ID, e.Owner.Key(), memberID+"/"+memberGeneration, func(token string) {
					current, err := r.Store.Execution(ctx, e.Owner, e.ID)
					valid := err == nil && current.Desired == "run"
					found := false
					for _, m := range current.Members {
						if m.ID == memberID && m.Generation == memberGeneration {
							found = true
						}
					}
					if !valid || !found {
						r.Gate.Done(p.ID, token)
						_ = p.Send(supervisor.Message{Kind: "permit", MemberID: memberID, MemberGeneration: memberGeneration})
						return
					}
					if err = p.Send(supervisor.Message{Kind: "permit", MemberID: memberID, MemberGeneration: memberGeneration, PermitID: token, Accepted: true}); err != nil {
						p.Stop()
					}
				}) {
					p.Stop()
				}
			case "inference_done":
				if r.Gate != nil {
					r.Gate.Done(p.ID, msg.PermitID)
				}
			case "ready":
				if !ready {
					ready = true
					_ = r.Store.Tx(ctx, func(tx *sql.Tx) error {
						current, err := store.Execution(ctx, tx, e.Owner, e.ID)
						if err != nil {
							return err
						}
						if current.State != "preparing" || current.Desired != "run" {
							return nil
						}
						current.State = "running"
						current.ObservedRevision = current.Revision
						for i := range current.Members {
							current.Members[i].State = "running"
						}
						if err = store.SaveExecution(tx, current); err != nil {
							return err
						}
						return store.UpdateOperation(tx, e.Owner, current.OperationID, "succeeded", current)
					})
				}
			case "event":
				if msg.Event == nil {
					p.Kill()
					return
				}
				var err error
				if !p.LeaseValid() {
					_ = r.retire(ctx, e, "failed", "lease_expired")
					err = domain.Error(409, "generation_retired", "Concesión vencida")
				} else {
					err = r.Event(ctx, e, *msg.Event)
				}
				var fault *domain.Fault
				if err == nil || errors.As(err, &fault) {
					_ = p.Send(supervisor.Message{Kind: "ack", EventID: msg.Event.ID, Accepted: err == nil})
				}
			case "status":
				if msg.State == "configured" {
					_ = r.Store.Tx(ctx, func(tx *sql.Tx) error {
						current, err := store.Execution(ctx, tx, e.Owner, e.ID)
						if err != nil {
							return err
						}
						if current.Desired != "run" || current.Revision != msg.Revision {
							return nil
						}
						current.ObservedRevision = msg.Revision
						if err = store.SaveExecution(tx, current); err != nil {
							return err
						}
						_, err = tx.Exec("UPDATE reservations SET memory_mib=? WHERE environment=? AND tenant_id=? AND execution_id=?", r.Catalog.Cost(current.Members), e.Environment, e.TenantID, e.ID)
						return err
					})
				}
				if msg.State == "completed" {
					_ = r.retire(ctx, e, "completed", "media_eof")
					p.Stop()
				}
			case "error":
				_ = r.retire(ctx, e, "failed", msg.Reason)
				p.Stop()
			}
		}
	}
}
func (r *Reconciler) Configure(ctx context.Context, e domain.Execution) error {
	p := r.Manager.Get(e.ID)
	if p == nil {
		return nil
	}
	return p.Send(supervisor.Message{Kind: "configure", Members: e.Members, Revision: e.Revision})
}
func (r *Reconciler) StopAdmissions() { r.mu.Lock(); r.stopping = true; r.mu.Unlock() }
