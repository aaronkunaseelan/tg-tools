# State machine

## Job states

- `pending`
- `leased`
- `retryable`
- `succeeded`
- `dead`
- `cancelled`

### Allowed job transitions

- `pending -> leased | dead | cancelled`
- `retryable -> leased | dead | cancelled`
- `leased -> retryable | succeeded | dead`
- terminal states do not transition further

## Attempt states

- `leased`
- `running`
- `succeeded`
- `retryable_error`
- `terminal_error`
- `expired`
- `abandoned`

### Allowed attempt transitions

- `leased -> running | succeeded | retryable_error | terminal_error | expired | abandoned`
- `running -> succeeded | retryable_error | terminal_error | expired | abandoned`
- terminal attempt states do not transition further

## Result acknowledgement lifecycle

Derived states:

- `ready`: `claimed_at = null`, `consumed_at = null`
- `claimed`: `claimed_at != null`, `consumed_at = null`
- `consumed`: `consumed_at != null`

## Delivery outbox states

- `pending`
- `claimed`
- `retryable`
- `sent`
- `dead`
- `cancelled`

### Allowed delivery transitions

- inserted rows start as `pending`
- `pending -> claimed | dead | cancelled`
- `retryable -> claimed | dead | cancelled`
- `claimed -> retryable | sent | dead | cancelled`
- terminal delivery states do not transition further

## Source receipt states

- `accepted`
- `submitted`
- `cancelled`

### Allowed source receipt transitions

- inserted rows start as `accepted`
- `accepted -> submitted | cancelled`
- `submitted` rows must have a linked `job_id`
- `submitted` and `cancelled` rows do not transition further

## Worker registry states

- `active`
- `draining`
- `drained`

Workers register or heartbeat as `active`, can be asked to drain, and should
stop taking new claims once they enter drain behavior.

## Key invariants

- one active lease-bearing attempt per job
- one terminal result row per terminal job
- terminal jobs must carry `terminal_result_id`
- `submission_mode = 'explicit_batch'` requires `input_item_count`
- stale workers lose authority when `lease_token` no longer matches
- delivery failures are retried through `runtime.delivery_outbox`; they do not
  move terminal jobs back into executable states
- source receipts record producer-resolved provenance only; route resolution
  stays outside runtime-controller
