-- Captured migration definition. The shadow table was kept in sync until the atomic rename.
CREATE TABLE sales._orders_new (
  id BIGINT PRIMARY KEY,
  customer_id BIGINT NOT NULL,
  amount_cents BIGINT NOT NULL,
  status VARCHAR(16) NOT NULL,
  note VARCHAR(160) NULL,
  tax_cents BIGINT NOT NULL
);

-- status_code mapping used by all three triggers:
-- 0 -> new, 1 -> paid, 2 -> shipped, 3 -> cancelled
-- tax_cents is integer floor(amount_cents * 7 / 100).
-- INSERT/UPDATE/DELETE on sales.orders mirror the same primary key into _orders_new.
-- UPDATE changes only the shadow columns whose old-table source columns changed;
-- amount_cents also changes tax_cents. Unrelated shadow columns are left untouched.

-- During the captured migration the shadow table was rebuilt once to place customer_id
-- immediately after amount_cents. The logical values did not change; only physical ordinals did.
ALTER TABLE sales._orders_new MODIFY COLUMN customer_id BIGINT NOT NULL AFTER amount_cents;

RENAME TABLE sales.orders TO sales.orders_archive,
             sales._orders_new TO sales.orders;
