package admission

import (
	"context"
	"database/sql"
	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

type Budget struct {
	MemoryMiB  int
	MaxWorkers int
}

func (b Budget) Reserve(ctx context.Context, tx *sql.Tx, e domain.Execution, cost int) error {
	if cost < 0 {
		return domain.Error(409, "unprofiled_model", "Perfil desconocido")
	}
	var global, own, workers, ownWorkers int
	var quotaJSON string
	if err := tx.QueryRowContext(ctx, "SELECT quota FROM tenants WHERE environment=? AND tenant_id=?", e.Environment, e.TenantID).Scan(&quotaJSON); err != nil {
		return err
	}
	q, err := store.Decode[domain.Quota](quotaJSON)
	if err != nil {
		return err
	}
	if err = tx.QueryRowContext(ctx, "SELECT COALESCE(SUM(memory_mib),0),COUNT(*) FROM reservations").Scan(&global, &workers); err != nil {
		return err
	}
	if err = tx.QueryRowContext(ctx, "SELECT COALESCE(SUM(memory_mib),0),COUNT(*) FROM reservations WHERE environment=? AND tenant_id=?", e.Environment, e.TenantID).Scan(&own, &ownWorkers); err != nil {
		return err
	}
	if global+cost > b.MemoryMiB || own+cost > q.MemoryMiB || workers >= b.MaxWorkers || ownWorkers >= q.Executions {
		return domain.Error(409, "capacity_wait", "Esperando capacidad o cuota")
	}
	_, err = tx.ExecContext(ctx, "INSERT INTO reservations VALUES(?,?,?,?,?)", e.Environment, e.TenantID, e.ID, cost, "held")
	return err
}
