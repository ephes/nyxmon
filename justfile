# Justfile for nyxmon project development

# Default recipe - show available commands
default:
    @just --list

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
    uv run mypy src/

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
dev:
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

# Build package
build:
    uv build

# Publish package to PyPI
publish: build
    uv publish

# Production deployment commands
deploy-staging:
    cd deploy && ansible-playbook deploy.yml --limit staging

deploy-production:
    cd deploy && ansible-playbook deploy.yml --limit production

deploy-macmini:
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
deploy-macos:
    cd deploy && ansible-playbook macos_deploy_backup.yml

# Help for deployment
deploy-help:
    @echo "Deployment commands:"
    @echo ""
    @echo "  just deploy-staging     # Deploy to staging server (with traefik)"
    @echo "  just deploy-production  # Deploy to production server (with traefik)"
    @echo "  just deploy-macmini     # Deploy to macmini (Linux, no traefik)"
    @echo "  just deploy-macos       # Deploy to macOS (legacy, for reference)"
    @echo ""
    @echo "Before deploying:"
    @echo "  1. Ensure you have built and published the latest package: just publish"
    @echo "  2. Check that ansible can connect: ansible -m ping <host>"
    @echo "  3. Review deploy/host_vars/<host>.yml for configuration"
    @echo ""
    @echo "For macmini: Access via http://macmini.local:8000 or Tailscale IP"