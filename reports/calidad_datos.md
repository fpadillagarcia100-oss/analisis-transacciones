# Data quality report

_Generated 2026-10-15 18:42 UTC by `python/calidad_datos.py` on the raw tables of `data/transacciones.db`._

## Summary

- **15 of 19 checks passed**; 3 critical, 1 warning and 0 info checks exceed their threshold.
- Raw transactions: **120,267**. After the cleaning rules in `sql/01_limpieza.sql`: **118,951** (1,316 rows removed, 1.09%).
- A check passes when the affected share is at or below its threshold. Thresholds are tolerances agreed for analytics use: 0% for rules that must never be broken.
- Critical failures distort headline metrics (volume, counts); they are excluded by the cleaning view and should be fixed at the source system.

## Results

| Status | Severity | Check | Affected | Population | % affected | Threshold |
|---|---|---|---:|---:|---:|---:|
| **FAIL** | critical | `duplicate_transactions` | 598 | 120,267 transactions | 0.50% | 0.1% |
| **FAIL** | critical | `future_timestamp` | 120 | 120,267 transactions | 0.10% | 0% |
| **FAIL** | critical | `negative_amount_not_reversal` | 239 | 120,267 transactions | 0.20% | 0% |
| PASS | critical | `category_domain` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `client_card_mismatch` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `currency_domain` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `null_key_fields` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `orphan_card_client` | 0 | 900 cards | 0.00% | 0% |
| PASS | critical | `orphan_transaction_card` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `orphan_transaction_client` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `status_domain` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | critical | `zero_amount` | 0 | 120,267 transactions | 0.00% | 0% |
| **FAIL** | warning | `null_merchant` | 359 | 120,267 transactions | 0.30% | 0.1% |
| PASS | warning | `currency_country_mismatch` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | warning | `reversal_positive_amount` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | warning | `transaction_before_card_issue` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | warning | `transaction_before_client_signup` | 0 | 120,267 transactions | 0.00% | 0% |
| PASS | info | `cards_without_transactions` | 4 | 900 cards | 0.44% | 5% |
| PASS | info | `clients_without_approved_transactions` | 2 | 300 clients | 0.67% | 5% |

## Check definitions

- `duplicate_transactions` (critical): Same card, timestamp, amount and merchant under a different transaccion_id (extra copies only).
- `future_timestamp` (critical): fecha_hora after the extraction cut-off (2026-09-30 23:59:59).
- `negative_amount_not_reversal` (critical): Negative amount on an approved/declined row (only reversals may be negative).
- `category_domain` (critical): Merchant category outside the 8 allowed values.
- `client_card_mismatch` (critical): Transaction's cliente_id differs from the owner of its card.
- `currency_domain` (critical): Currency outside the allowed domain (MXN, COP, BRL, USD).
- `null_key_fields` (critical): NULL in a mandatory column (tarjeta_id, cliente_id, fecha_hora, monto, moneda, categoria_comercio, estatus).
- `orphan_card_client` (critical): Card whose cliente_id does not exist in clientes.
- `orphan_transaction_card` (critical): Transaction whose tarjeta_id does not exist in tarjetas.
- `orphan_transaction_client` (critical): Transaction whose cliente_id does not exist in clientes.
- `status_domain` (critical): Status outside the allowed domain (aprobada, rechazada, reversada).
- `zero_amount` (critical): Amount equal to zero (meaningless for a card payment).
- `null_merchant` (warning): Merchant name (comercio) is NULL: the row cannot be attributed or deduplicated.
- `currency_country_mismatch` (warning): Currency is neither USD nor the local currency of the client's country.
- `reversal_positive_amount` (warning): Reversal recorded with a positive amount (would double-count spend).
- `transaction_before_card_issue` (warning): Transaction dated before the card's issue date.
- `transaction_before_client_signup` (warning): Transaction dated before the client's signup date.
- `cards_without_transactions` (info): Issued cards with no transaction at all (activation gap, not an error).
- `clients_without_approved_transactions` (info): Clients that never completed an approved transaction.
