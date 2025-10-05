# Justfile for nyxmon project development

# ========= Config (override via .envrc or environment) =========
# Path to your local ops-control clone
OPS_CONTROL := env_var_or_default("OPS_CONTROL", "/Users/jochen/projects/ops-control")

# Remote host and SSH user from ops-control inventory
HOST := env_var_or_default("HOST", "macmini")
USER := env_var_or_default("USER", "root")

# Limit (inventory hostnames) used by site.yml
LIMIT := env_var_or_default("LIMIT", "macmini,localhost")

# Optional: which Ansible to call (venv etc.)
ANSIBLE_PLAYBOOK := env_var_or_default("ANSIBLE_PLAYBOOK", "ansible-playbook")
ANSIBLE := env_var_or_default("ANSIBLE", "ansible")

# Default recipe - show available commands
default:
    @just --list

# ========= Development Commands =========

# Install dependencies
install:
    uv sync

# Package management
add PACKAGE:
    uv pip install {{PACKAGE}}

remove PACKAGE:
    uv pip uninstall {{PACKAGE}}

# Lint code
lint:
    uvx pre-commit run --all-files

# Type check
typecheck:
    uv run mypy

# Run all tests
test:
    uv run pytest

# Run specific test
test-one TEST_PATH:
    uv run pytest {{TEST_PATH}} -v

# Django management commands
manage *ARGS:
    cd src/django && uv run python manage.py {{ARGS}}

# Run Django development server
dev-server:
    cd src/django && uv run python manage.py runserver

# Database operations
db-migrate:
    @just manage makemigrations
    @just manage migrate

db-shell:
    @just manage dbshell

# Shell access
shell:
    @just manage shell_plus

# Build package (wheel and sdist separately to avoid hatchling sdist-to-wheel issues)
build:
    @echo "Building wheel and sdist separately..."
    uv build --wheel && uv build --sdist
    @echo ""
    @echo "Built packages:"
    @ls -lh dist/

# Publish package to PyPI
publish: build
    uv publish

# ========= Documentation Commands =========

# Build documentation
docs-build:
    uv run sphinx-build -b html docs docs/_build/html

# View documentation locally
docs-serve:
    uv run python -m http.server 8000 --directory docs/_build/html

# Clean documentation build artifacts
docs-clean:
    rm -rf docs/_build

# Check documentation for broken links
docs-check:
    uv run sphinx-build -b linkcheck docs docs/_build/linkcheck

# Watch mode for documentation with auto rebuild
docs-watch:
    uv run sphinx-autobuild docs docs/_build/html

# ========= ops-control Deployment Commands =========

# Deploy: run Ansible from laptop, syncing current working tree
deploy:
    #!/usr/bin/env bash
    cd {{OPS_CONTROL}} && \
    {{ANSIBLE_PLAYBOOK}} \
      -i inventories/prod/hosts.yml \
      playbooks/site.yml \
      -l {{LIMIT}} \
      --extra-vars 'filter=nyxmon' \
      --extra-vars "rsync_src_override=$PWD/src/django/" \
      --extra-vars "rsync_additional_src_override=$PWD/src/nyxboard/"

# Optional: push code only (no playbook), handy for quick file transfers
push:
    rsync -az --delete \
      --exclude ".git" --exclude "__pycache__" --exclude "*.pyc" \
      --exclude ".venv" --exclude "venv" --exclude "db.sqlite3" \
      --exclude "cache" --exclude "staticfiles" \
      src/django/ {{USER}}@{{HOST}}:/opt/apps/nyxmon/site/
    rsync -az --delete \
      --exclude "__pycache__" --exclude "*.pyc" \
      src/nyxboard/ {{USER}}@{{HOST}}:/opt/apps/nyxmon/site/nyxboard/

# Dev loop convenience: deploy and follow logs
dev:
    just deploy
    just logs-follow

# Show recent service logs (without follow)
logs LINES="100":
    cd {{OPS_CONTROL}} && \
    {{ANSIBLE}} -i inventories/prod/hosts.yml {{HOST}} \
      -m shell -a "journalctl -u nyxmon -n {{LINES}} -o short-iso"

# Follow service logs in real-time (requires SSH)
logs-follow:
    ssh {{USER}}@{{HOST}} "journalctl -u nyxmon -f -o short-iso"

# Restart nyxmon service via systemd
restart:
    cd {{OPS_CONTROL}} && \
    {{ANSIBLE}} -i inventories/prod/hosts.yml {{HOST}} \
      -m systemd -a "name=nyxmon state=restarted" --become

# Show service status
status:
    cd {{OPS_CONTROL}} && \
    {{ANSIBLE}} -i inventories/prod/hosts.yml {{HOST}} \
      -m shell -a "systemctl status nyxmon --no-pager || true"

# Bootstrap: Install Ansible collections for ops-control (run once)
bootstrap-ops:
    cd {{OPS_CONTROL}} && \
      ansible-galaxy collection install -r collections/requirements.yml -p ./collections

# ========= Legacy Deployment Commands (for reference) =========
# These commands use the old deployment method via local playbooks in deploy/

deploy-staging-legacy:
    cd deploy && ansible-playbook deploy.yml --limit staging

deploy-production-legacy:
    cd deploy && ansible-playbook deploy.yml --limit production

deploy-macmini-legacy:
    cd deploy && ansible-playbook linux_macmini_deploy.yml

# Backup database from production
backup-production:
    cd deploy && ansible-playbook backup_database.yml --limit production

# Remove deployment
remove-staging:
    cd deploy && ansible-playbook remove.yml --limit staging

remove-production:
    cd deploy && ansible-playbook remove.yml --limit production

remove-macmini:
    cd deploy && ansible-playbook remove.yml --limit macmini

# Deploy to macOS (legacy - for reference)
deploy-macos-legacy:
    cd deploy && ansible-playbook macos_deploy_backup.yml

# Help for deployment
deploy-help:
    @echo "ops-control Deployment Commands:"
    @echo ""
    @echo "  just deploy         # Deploy to macmini using ops-control (recommended)"
    @echo "  just push           # Quick rsync without full deployment"
    @echo "  just logs [N]       # Show last N lines of service logs (default: 100)"
    @echo "  just logs-follow    # Follow service logs in real-time"
    @echo "  just restart        # Restart the nyxmon service"
    @echo "  just status         # Check service status"
    @echo "  just dev            # Deploy and follow logs (dev workflow)"
    @echo "  just bootstrap-ops  # Install Ansible collections (first time only)"
    @echo ""
    @echo "Legacy Deployment Commands (old method):"
    @echo ""
    @echo "  just deploy-staging-legacy     # Deploy to staging (old method)"
    @echo "  just deploy-production-legacy  # Deploy to production (old method)"
    @echo "  just deploy-macmini-legacy     # Deploy to macmini (old method)"
    @echo ""
    @echo "Configuration:"
    @echo "  Edit .envrc to change deployment settings"
    @echo "  Current target: {{HOST}} ({{USER}}@{{HOST}})"
    @echo "  ops-control path: {{OPS_CONTROL}}"
    @echo ""
    @echo "For macmini: Access via http://macmini.local:8000 or Tailscale IP"