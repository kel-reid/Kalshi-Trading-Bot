# Project Rules

- Do not perform Git commits under any circumstances unless explicitly instructed to do so by the user.
- Always run the full pytest test suite (`.venv/bin/pytest -v`) and application verification checks before code is committed.
- Always remind the user to run the local `terraform validate` syntax check and `tfsec` security scan commands whenever changes are made to the Terraform configurations.

