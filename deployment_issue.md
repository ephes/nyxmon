# Production Deployment Setup for nyxmon

## Overview
Updated deployment configuration to:
1. Replace references to "homepage" with "nyxmon"
2. Use granian instead of gunicorn as WSGI server
3. Install nyxmon package from PyPI rather than copying source files
4. Remove PostgreSQL dependency and use SQLite instead
5. Fix path configuration in Django settings for production

## Completed Tasks

### 1. Update deployment files
- [x] Updated `deploy/vars.yml` to change project_name from "homepage" to "nyxmon"
- [x] Removed PostgreSQL setup from deployment files
- [x] Updated Ansible playbook to focus on Django and nyxmon package

### 2. Switch from gunicorn to granian
- [x] Updated `deploy/templates/systemd.service.j2` to reference granian
- [x] Created new `deploy/templates/granian.sh.j2` to replace gunicorn script
- [x] Changed `gunicorn_number_of_workers` to `granian_number_of_workers` in vars.yml

### 3. Modified deployment process for PyPI installation
- [x] Updated `deploy.yml` to install nyxmon from PyPI using uv sync
- [x] Added new file-copying approach that only copies Django configuration
- [x] Updated migrations and collectstatic paths to work with new deployment structure

### 4. Configured Django deployment
- [x] Created template pyproject.toml.j2 for deployment with:
  - Set name to "nyxmon-site" with version "0"
  - Set Python requirement to "==3.13.*"
  - Added nyxmon and granian as dependencies
- [x] Added step to template this file during deployment

### 5. Documentation
- [x] Updated CLAUDE.md with production deployment instructions
- [x] Added deployment commands to nyxmon entrypoints
- [x] Updated CLAUDE.md with notes about the deployment process

### 6. Created deployment utilities
- [x] Added deployment-related functions to nyxmon.entrypoints.deployment
- [x] Created CLI commands for deploy-staging, deploy-production and db-from-production
- [x] Updated pyproject.toml to include these new entry points

### 7. Fixed path configuration for production
- [x] Created production_paths.py.j2 template to override Django path settings
- [x] Updated production.py settings to import from production_paths if available
- [x] Modified deploy.yml to create production_paths.py during deployment
- [x] Fixed database configuration for SQLite in production

## Technical Notes
- Django configuration is copied from src/django to the remote site_path
- nyxmon package is installed from PyPI using uv sync during deployment
- Deployment process requires the built and published nyxmon package
- granian is used instead of gunicorn for better performance
- Django database uses SQLite instead of PostgreSQL
- The CLI commands deploy-staging, deploy-production, and db-from-production are available from the nyxmon package