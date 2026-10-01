# Project Rules

- Do not perform Git commits under any circumstances unless explicitly instructed to do so by the user.
- Always run the full pytest test suite (`.venv/bin/pytest -v`) and application verification checks before code is committed.
- Always remind the user to run the local `terraform validate` syntax check and `tfsec` security scan commands whenever changes are made to the Terraform configurations.
- Never alter external API query parameters, response payload validations, or endpoint URLs based on static code review suggestions or test fixture inconsistencies without empirical verification against the live vendor API. When multi-value status predicates exist (e.g. `status in ("open", "active")`), treat them as intentional vendor API quirks unless proven otherwise by live telemetry; always preserve both values in parsing and unit tests.
- Whenever fixes addressing PR review comments are committed and pushed, always mark the corresponding GitHub review threads as resolved via the GitHub API.
- Whenever code is committed and pushed to a PR branch, always request a CodeRabbit review by posting `@coderabbit full review` on the pull request. If the review was not started immediately (e.g. due to rate limits or fair usage policies), inspect CodeRabbit's response comment for when the next review will become available, wait for that duration, and re-trigger `@coderabbit full review` once the window opens until confirmed started ("Full review triggered").
- **Strict SDLC & Production Deployment Protocol**: Never manually deploy, copy files, or patch the production DigitalOcean droplet via ad-hoc SSH/SCP from local feature branches or unmerged code under any circumstances. All production deployments must strictly flow through the automated GitHub Actions CI/CD pipeline triggered by merging approved pull requests into `main`. Pre-merge validation must strictly remain within local unit/integration tests (`.venv/bin/pytest -v`), local Terraform checks (`terraform validate` and `tfsec`) when Terraform configurations change, and GitHub Actions CI checks.
- After each successful production deploy executed by the GitHub Actions CI/CD pipeline to DigitalOcean, always inspect the droplet container logs (e.g. via SSH `ssh -i infra/kalshi_deploy_key.pem root@<DROPLET_IP> "docker logs --tail 100 kalshi-bot"`) to empirically verify startup hydration, market discovery, and healthy quoting without runtime errors.

## Senior Engineering Protocols

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
