UV ?= uv
DOCKER ?= docker
IMAGE ?= alfred
REGISTRY ?= album.lan
# Extra flags for push, e.g. PUSH_FLAGS=--tls-verify=false (podman only).
PUSH_FLAGS ?=
VERSION := $(shell sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
# Podman drops HEALTHCHECK from OCI-format images; ask it for Docker format.
BUILD_FLAGS ?= $(if $(shell $(DOCKER) --version 2>/dev/null | grep -i podman),--format docker,)

.PHONY: install lint format test run image push

install:
	$(UV) sync

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test:
	$(UV) run pytest

# Reads alfred.toml (or $$ALFRED_CONFIG) and the ALFRED_* secrets from the environment.
run:
	$(UV) run alfred serve

image:
	$(DOCKER) build $(BUILD_FLAGS) -t $(IMAGE):$(VERSION) -t $(IMAGE):latest .

# Builds first so the pushed image always matches the working tree.
# Needs `docker login $(REGISTRY)` and a machine that trusts the registry's CA.
push: image
	@for tag in $(VERSION) latest; do \
		echo "pushing $(REGISTRY)/$(IMAGE):$$tag"; \
		$(DOCKER) tag $(IMAGE):$$tag $(REGISTRY)/$(IMAGE):$$tag || exit 1; \
		$(DOCKER) push $(PUSH_FLAGS) $(REGISTRY)/$(IMAGE):$$tag || exit 1; \
	done
