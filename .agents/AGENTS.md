# Project Rules

- Do not perform Git commits under any circumstances unless explicitly instructed to do so by the user.
- Always run the full pytest test suite (`.venv/bin/pytest -v`) and application verification checks before code is committed.
- Always remind the user to run the local `terraform validate` syntax check and `tfsec` security scan commands whenever changes are made to the Terraform configurations.
- Never alter external API query parameters, response payload validations, or endpoint URLs based on static code review suggestions or test fixture inconsistencies without empirical verification against the live vendor API. When multi-value status predicates exist (e.g. `status in ("open", "active")`), treat them as intentional vendor API quirks unless proven otherwise by live telemetry; always preserve both values in parsing and unit tests.
- Always write and review code from the perspective of a senior engineer: prioritize production reliability, architectural clarity, defensive error handling, deterministic test coverage, operability, and empirical verification over superficial or speculative fixes.

## Senior Engineering Operational Protocols

- **Anti-Regression & Invariant Tracing Protocol**: When addressing code review comments, bug reports, or edge cases, never perform isolated line edits that only address the cited lines. Explicitly trace and verify the change across the entire system:
  1. Validate boundary preconditions before mutating any state.
  2. Maintain conserved identities (e.g., `Total PnL = Realized + Unrealized`) across all components.
  3. Verify behavior across all lifecycle phases: cold startup, active trading, background reconciliation, and shutdown.
- **Financial Accounting Invariants**: In all inventory, PnL, and balance calculations:
  - $\text{Total PnL} = \text{Realized PnL} + \text{Unrealized PnL}$ must strictly hold at all times.
  - Fees and cost basis must never disappear or be double-counted. Deferred entry fees on open lots must be accounted for in unrealized mark-to-market calculations so Total Strategy PnL is always exact.
- **Boundary Validation Uniformity**: Validate and drop malformed payloads (`count <= 0`, missing tickers) at the outermost ingress method before internal state, counters, or balances are updated. Ensure all layers enforce the same domain validation rules.
- **Complete Lifecycle Gating & Test Mock Audits**:
  - Gating mechanisms protecting trading operations must require the *complete set* of ground truths (e.g., both balance and positions verified) before quoting can begin.
  - When modifying lifecycle preconditions, audit all test harnesses to ensure mock configurations explicitly satisfy contracts rather than masking assertions.
- **Adversarial Invariant Testing**: Write deterministic unit tests that assert domain invariants through state transitions (open, partial close, reversal, full close) and negative tests for each prerequisite failure path.
