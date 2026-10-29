package reconcile

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

func (r *Reconciler) settle(ctx context.Context) error {
	return r.Store.Tx(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "SELECT data FROM operations WHERE json_extract(data,'$.state')='pending'")
		if err != nil {
			return err
		}
		ops := []domain.Operation{}
		for rows.Next() {
			var d string
			if err = rows.Scan(&d); err != nil {
				rows.Close()
				return err
			}
			op, err := store.Decode[domain.Operation](d)
			if err != nil {
				rows.Close()
				return err
			}
			ops = append(ops, op)
		}
		rows.Close()
		for _, op := range ops {
			if strings.Contains(op.Kind, "/executions/{id}/stop") {
				e, err := store.Execution(ctx, tx, op.Owner, op.ResourceID)
				if err != nil {
					return err
				}
				if !e.Active() {
					if err = store.UpdateOperation(tx, op.Owner, op.ID, "succeeded", e); err != nil {
						return err
					}
				}
				continue
			}
			if strings.Contains(op.Kind, "/cameras/{id}/executions") {
				continue
			}
			if !strings.Contains(op.Kind, "/cameras/{id}") {
				continue
			}
			c, err := store.Camera(ctx, tx, op.Owner, op.ResourceID)
			if err != nil {
				return err
			}
			var data string
			err = tx.QueryRowContext(ctx, "SELECT data FROM executions WHERE environment=? AND tenant_id=? AND camera_id=? AND state IN ('queued','preparing','running','stopping')", c.Environment, c.TenantID, c.ID).Scan(&data)
			if err == sql.ErrNoRows {
				if err = store.UpdateOperation(tx, op.Owner, op.ID, "succeeded", c); err != nil {
					return err
				}
				continue
			}
			if err != nil {
				return err
			}
			e, err := store.Decode[domain.Execution](data)
			if err != nil {
				return err
			}
			if strings.Contains(op.Kind, "composition") || strings.Contains(op.Kind, "members") {
				b, err := json.Marshal(op.Result)
				if err != nil {
					return err
				}
				var target domain.Camera
				if err = json.Unmarshal(b, &target); err != nil {
					return err
				}
				if e.ObservedRevision >= target.Revision {
					if err = store.UpdateOperation(tx, op.Owner, op.ID, "succeeded", c); err != nil {
						return err
					}
				}
			}
		}
		return nil
	})
}
