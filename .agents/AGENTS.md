# Project Rules

- Do not perform Git commits under any circumstances unless explicitly instructed to do so by the user.
- Always run the full pytest test suite (`.venv/bin/pytest -v`) and application verification checks before code is committed.
- Always remind the user to run the local `terraform validate` syntax check and `tfsec` security scan commands whenever changes are made to the Terraform configurations.
- Never alter external API query parameters, response payload validations, or endpoint URLs based on static code review suggestions or test fixture inconsistencies without empirical verification against the live vendor API. When multi-value status predicates exist (e.g. `status in ("open", "active")`), treat them as intentional vendor API quirks unless proven otherwise by live telemetry; always preserve both values in parsing and unit tests.
- Always write and review code from the perspective of a senior engineer: prioritize production reliability, architectural clarity, defensive error handling, deterministic test coverage, operability, and empirical verification over superficial or speculative fixes.


