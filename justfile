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

# Lint code (Python pre-commit hooks, then the Rust agent)
lint:
    uvx pre-commit run --all-files
    just rs-lint

# Type check (mypy, then cargo check of the Rust agent)
typecheck:
    uv run mypy
    cd agent-rs && cargo check --workspace --all-targets --locked

# Run all tests (Python suite incl. the Rust fixture drift test, then the Rust agent)
test:
    uv run pytest
    just rs-test

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

# ========= Rust agent (agent-rs/, see agent-rs/README.md) =========
# Recipes cd into agent-rs so rustup picks up agent-rs/rust-toolchain.toml.

# Rust agent tests
rs-test:
    cd agent-rs && cargo test --workspace --locked

# Rust agent lint: rustfmt check and clippy with warnings as errors
rs-lint:
    cd agent-rs && cargo fmt --all --check
    cd agent-rs && cargo clippy --workspace --all-targets --locked -- -D warnings

# Format the Rust agent
rs-fmt:
    cd agent-rs && cargo fmt --all

# Regenerate the shared fixtures and the schema contract from the Python implementation
rs-fixtures:
    uv run python agent-rs/fixtures/generate.py
    uv run python agent-rs/fixtures/generate_schema.py

# Release build of the Rust agent for this host
rs-build:
    cd agent-rs && cargo build --release --locked

# Release build for macmini (x86_64 glibc) in Docker; output in agent-rs/target/docker-linux-amd64
rs-build-linux:
    docker run --rm --platform linux/amd64 \
      -v "{{justfile_directory()}}/agent-rs:/work" -w /work \
      -v nyxmon-agent-rs-cargo-registry:/usr/local/cargo/registry \
      -e CARGO_TARGET_DIR=/work/target/docker-linux-amd64 \
      rust:1.99.0-bookworm \
      cargo build --release --locked --target x86_64-unknown-linux-gnu

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
    SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \
    {{ANSIBLE_PLAYBOOK}} \
      -i inventories/prod/hosts.yml \
      playbooks/deploy-nyxmon.yml

# Remove: completely remove Nyxmon from the target host
remove:
    #!/usr/bin/env bash
    echo "⚠️  WARNING: This will completely remove Nyxmon from {{HOST}}"
    echo "This includes all monitoring data, database, users, and configuration!"
    echo ""
    read -p "Are you ABSOLUTELY sure? Type 'REMOVE' to confirm: " confirm
    if [ "$confirm" = "REMOVE" ]; then
        cd {{OPS_CONTROL}} && \
        SOPS_AGE_KEY_FILE=~/.config/sops/age/keys.txt \
        {{ANSIBLE_PLAYBOOK}} \
          -i inventories/prod/hosts.yml \
          playbooks/remove-nyxmon.yml \
          -l {{HOST}} \
          -e nyxmon_confirm_removal=true
    else
        echo "Removal cancelled"
    fi

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
    @echo "Configuration:"
    @echo "  Edit .envrc to change deployment settings"
    @echo "  Current target: {{HOST}} ({{USER}}@{{HOST}})"
    @echo "  ops-control path: {{OPS_CONTROL}}"
