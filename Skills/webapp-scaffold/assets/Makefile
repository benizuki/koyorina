# 社内レジストリ向けの build / push。
# Cloud Build を使う場合は cloudbuild.yaml だけで足りるので、この Makefile は不要。
SHELL := /bin/sh

DOCKER ?= docker
BUILDX ?= $(DOCKER) buildx
# プライベート CA を使うレジストリでは、その CA を仕込んだ BuildKit builder が要る。
# 該当する場合は BUILDKITD_CONFIG=/absolute/path/buildkitd.toml を指定する。
BUILDER ?= __APP_NAME__-builder
BUILDKITD_CONFIG ?= $(HOME)/buildkitd-registry.toml

REGISTRY ?= registry.example.com
IMAGE_ROOT ?= $(REGISTRY)
IMAGE_NAME ?= __APP_NAME__
IMAGE ?= $(IMAGE_ROOT)/$(IMAGE_NAME)
CACHE_REPOSITORY ?= $(IMAGE_ROOT)/build-cache
TAG ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
LATEST_TAG ?= latest
PLATFORM ?= linux/amd64

.PHONY: help image ensure-builder recreate-builder build push build-push

help:
	@printf '%s\n' \
		'make build         ローカル Docker にイメージをビルド' \
		'make push          ローカルにあるイメージを push' \
		'make build-push    buildx でビルドして直接 push' \
		'make recreate-builder  BuildKit builder を再作成' \
		'make image         対象イメージ名を表示' \
		'' \
		'主な変数: REGISTRY=registry.example.com IMAGE_ROOT=registry.example.com/team' \
		'          IMAGE_NAME=__APP_NAME__ TAG=<version> PLATFORM=linux/amd64'

image:
	@printf '%s\n' '$(IMAGE):$(TAG)'

ensure-builder:
	@set -eu; \
	if ! $(BUILDX) inspect "$(BUILDER)" >/dev/null 2>&1; then \
		echo "==> Creating Buildx builder $(BUILDER)"; \
		if [ -n "$(BUILDKITD_CONFIG)" ] && [ -f "$(BUILDKITD_CONFIG)" ]; then \
			$(BUILDX) create --name "$(BUILDER)" --driver docker-container --use --bootstrap --buildkitd-config "$(BUILDKITD_CONFIG)" >/dev/null; \
		else \
			$(BUILDX) create --name "$(BUILDER)" --driver docker-container --use --bootstrap >/dev/null; \
		fi; \
	fi

# BuildKit の設定は builder 作成時にしか読まれない。CA を足したら必ずこれを実行する。
recreate-builder:
	@set -eu; \
	if [ -z "$(BUILDKITD_CONFIG)" ] || [ ! -f "$(BUILDKITD_CONFIG)" ]; then \
		echo "Set BUILDKITD_CONFIG to an existing BuildKit configuration file." >&2; exit 2; \
	fi; \
	if $(BUILDX) inspect "$(BUILDER)" >/dev/null 2>&1; then \
		$(BUILDX) rm --force "$(BUILDER)" >/dev/null; \
	fi; \
	$(MAKE) --no-print-directory ensure-builder

build: ensure-builder
	@set -eu; \
	ref="$(IMAGE):$(TAG)"; \
	tag_args="-t $$ref"; \
	if [ "$(TAG)" != "$(LATEST_TAG)" ]; then tag_args="$$tag_args -t $(IMAGE):$(LATEST_TAG)"; fi; \
	cache_ref="$(CACHE_REPOSITORY)/$(IMAGE_NAME):buildcache"; \
	echo "==> Building $$ref"; \
	$(BUILDX) build --builder "$(BUILDER)" --platform "$(PLATFORM)" \
		--cache-from "type=registry,ref=$$cache_ref" \
		--cache-to "type=registry,ref=$$cache_ref,mode=max" \
		--load $$tag_args .

push:
	@set -eu; \
	echo "==> Pushing $(IMAGE):$(TAG)"; $(DOCKER) push "$(IMAGE):$(TAG)"; \
	if [ "$(TAG)" != "$(LATEST_TAG)" ]; then \
		$(DOCKER) push "$(IMAGE):$(LATEST_TAG)"; \
	fi

build-push: ensure-builder
	@set -eu; \
	ref="$(IMAGE):$(TAG)"; \
	tag_args="-t $$ref"; \
	if [ "$(TAG)" != "$(LATEST_TAG)" ]; then tag_args="$$tag_args -t $(IMAGE):$(LATEST_TAG)"; fi; \
	cache_ref="$(CACHE_REPOSITORY)/$(IMAGE_NAME):buildcache"; \
	echo "==> Building and pushing $$ref"; \
	$(BUILDX) build --builder "$(BUILDER)" --platform "$(PLATFORM)" \
		--cache-from "type=registry,ref=$$cache_ref" \
		--cache-to "type=registry,ref=$$cache_ref,mode=max" \
		--push $$tag_args .
