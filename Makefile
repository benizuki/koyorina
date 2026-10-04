SHELL := /bin/sh

DOCKER ?= docker
BUILDX ?= $(DOCKER) buildx

# ドメインとイメージの置き場は setup/environments/ が正。ここに書き写さない。
# ENV=prod make build のように、環境を変えるのは ENV だけで済ませる。
ENV ?= dev

# 設定ファイルが無いまま進むと、load.pyのエラーが変数展開に埋もれて
# 「/:<tag>」のような壊れたイメージ名になる。配備系のターゲットだけ、
# 何を作ればよいかを示して早く止める。
CONFIG_TARGETS := login images digests build push build-push pin
ifneq ($(filter $(MAKECMDGOALS),$(CONFIG_TARGETS)),)
ifeq ($(wildcard setup/environments/$(ENV).env),)
$(error setup/environments/$(ENV).env がありません。まず `cp setup/environments/example.env setup/environments/$(ENV).env` を実行し、値を編集してください)
endif
endif

APP_NAME ?= $(shell python3 setup/environments/load.py $(ENV) --value APP_NAME)
BUILDER ?= $(APP_NAME)-builder
BUILDKITD_CONFIG ?= $(HOME)/buildkitd-registry.toml
REGISTRY ?= $(shell python3 setup/environments/load.py $(ENV) --value REGISTRY)

# Set IMAGE_ROOT when the registry uses a namespace, for example:
# IMAGE_ROOT=registry.example.com/koyorina
IMAGE_ROOT ?= $(shell python3 setup/environments/load.py $(ENV) --value IMAGE_ROOT)
CACHE_REPOSITORY ?= $(IMAGE_ROOT)/build-cache
CACHE ?= 1
AUTO_TAG := $(shell git rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)
TAG ?= $(AUTO_TAG)
LATEST_TAG ?= latest
PUSH_LATEST ?= 1

# k3sはlinux/amd64。手元がarmでも配備先に合わせる。
PLATFORM ?= linux/amd64
# 本体イメージのDockerfile。Takumi Images 版を使うなら Dockerfile.takumi。
APP_DOCKERFILE ?= Dockerfile

ALL_SERVICES := app agent preview-runtime
SERVICES ?= $(ALL_SERVICES)

SERVICE_SHORTCUTS := $(ALL_SERVICES)

.PHONY: help login list images digests build push build-push \
	pin compose-up compose-down compose-logs \
	$(addprefix build-,$(SERVICE_SHORTCUTS)) \
	$(addprefix push-,$(SERVICE_SHORTCUTS)) \
	$(addprefix build-push-,$(SERVICE_SHORTCUTS))

help:
	@printf '%s\n' \
		'make compose-up                             お試し版をDocker Composeで起動（http://localhost:8080）' \
		'make compose-down                           お試し版を停止（データは残る）' \
		'make compose-logs                           お試し版のログを見る' \
		'' \
		'make login                                  Dockerレジストリへログイン' \
		'make build                                 全イメージをビルド' \
		'make push                                  ビルド済みイメージをPush' \
		'make build-push                            全イメージをビルドしてPush' \
		'make pin                                   Push済みdigestをマニフェストへ反映' \
		'make build-agent                           agentだけビルド' \
		'make build-push-agent                      agentだけビルドしてPush' \
		'make list                                  対象サービス一覧' \
		'make images                                生成されるイメージ名一覧' \
		'make digests                               Push済みイメージのdigestを表示' \
		'' \
		'環境: ENV=dev（既定）／ ENV=prod。ドメイン・push先・GCPプロジェクトは' \
		'      setup/environments/<環境>.env が正。Makefileにも各マニフェストにも書き写さない。' \
		'      例: ENV=prod make build-push  ／  ENV=prod setup/manifest/apply.sh' \
		'' \
		'サービス: app（管理アプリ）／ agent（生成を実行する）／ preview-runtime（生成アプリの実行環境）' \
		'seccompプロファイルはイメージにしない。GCEの起動スクリプトがノードへ置く（setup/gcp/terraform）。' \
		'配備はdigest固定。Push後に make pin でマニフェストのdigestを差し替える。' \
		'貼り忘れると apply.sh は前と同じイメージを当て直し、何も変わらないのに成功する:' \
		'  make build-push && make pin && setup/manifest/apply.sh' \
		'  make pin は app / agent / preview-runtime の3つとも差し替える' \
		'注意: 生成規約 backend/conventions/ は app と agent のイメージに入る。' \
		'      同梱版を変えるにはイメージの作り直しが要る。作り直さずに差し替えるなら' \
		'      CONVENTIONS_ROOT へ AGENTS.md と scaffold/ を置いた場所を指す。' \
		'' \
		'TAG: 未指定時はGitの短縮コミットSHA（TAG=devで上書き可能）' \
		'Push: PUSH_LATEST=1でlatestも更新（PUSH_LATEST=0で無効）' \
		'変数: IMAGE_ROOT=registry.example.com LATEST_TAG=latest PLATFORM=linux/amd64' \
		'本体のDockerfile: APP_DOCKERFILE=Dockerfile（標準・既定）／ Dockerfile.takumi（Takumi Images）' \
		'キャッシュ: CACHE_REPOSITORY=registry.example.com/build-cache（CACHE=0で無効）' \
		'自己証明書: BUILDER=... BUILDKITD_CONFIG=/absolute/path/buildkitd.toml' \
		'例: make build-push SERVICES="app agent" TAG=$$(git rev-parse --short HEAD)' \
		'' \
		'反映: digestをマニフェストへ貼ったあと、setup/manifest/apply.sh で3か所へまとめて当てる。' \
		'      管理アプリ・プレビュー・生成のどれか1つを当て忘れると、修正が届かない場所が残る。'

login:
	$(DOCKER) login $(REGISTRY)

list:
	@printf '%s\n' $(SERVICES)

images:
	@set -eu; \
	for service in $(SERVICES); do \
		case "$$service" in \
			app) image="$(APP_NAME)";; \
			agent) image="$(APP_NAME)-agent";; \
			preview-runtime) image="$(APP_NAME)-preview-runtime";; \
			*) echo "Unknown service: $$service" >&2; exit 2;; \
		esac; \
		printf '%s/%s:%s\n' "$(IMAGE_ROOT)" "$$image" "$(TAG)"; \
	done

digests:
	@set -eu; \
	for service in $(SERVICES); do \
		case "$$service" in \
			app) image="$(APP_NAME)";; \
			agent) image="$(APP_NAME)-agent";; \
			preview-runtime) image="$(APP_NAME)-preview-runtime";; \
			*) echo "Unknown service: $$service" >&2; exit 2;; \
		esac; \
		ref="$(IMAGE_ROOT)/$$image:$(TAG)"; \
		digest="$$($(BUILDX) imagetools inspect "$$ref" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"; \
		if [ -z "$$digest" ]; then \
			printf '%s  (未Push)\n' "$$ref"; \
		else \
			printf '%s@%s\n' "$(IMAGE_ROOT)/$$image" "$$digest"; \
		fi; \
	done

build:
	@set -eu; \
	for service in $(SERVICES); do \
		case "$$service" in \
			app) context="."; dockerfile="$(APP_DOCKERFILE)"; image="$(APP_NAME)";; \
			agent) context="."; dockerfile="setup/manifest/Dockerfile.agent"; image="$(APP_NAME)-agent";; \
			preview-runtime) context="setup/preview"; dockerfile="setup/preview/Dockerfile.runtime"; image="$(APP_NAME)-preview-runtime";; \
			*) echo "Unknown service: $$service" >&2; exit 2;; \
		esac; \
		if ! $(BUILDX) inspect "$(BUILDER)" >/dev/null 2>&1; then \
			echo "==> Creating Buildx builder $(BUILDER)"; \
			if [ -n "$(BUILDKITD_CONFIG)" ] && [ -f "$(BUILDKITD_CONFIG)" ]; then \
				$(BUILDX) create --name "$(BUILDER)" --driver docker-container --use --bootstrap --buildkitd-config "$(BUILDKITD_CONFIG)" >/dev/null; \
			else \
				$(BUILDX) create --name "$(BUILDER)" --driver docker-container --use --bootstrap >/dev/null; \
			fi; \
		fi; \
		ref="$(IMAGE_ROOT)/$$image:$(TAG)"; \
		cache_ref="$(CACHE_REPOSITORY)/$$image:buildcache"; \
		cache_args=""; \
		if [ "$(CACHE)" = "1" ]; then \
			cache_args="--cache-from type=registry,ref=$$cache_ref --cache-to type=registry,ref=$$cache_ref,mode=max"; \
		fi; \
		echo "==> Building $$ref"; \
		$(BUILDX) build \
			--builder "$(BUILDER)" \
			--platform "$(PLATFORM)" \
			--provenance=false \
			$$cache_args \
			--load \
			-f "$$dockerfile" \
			-t "$$ref" \
			"$$context"; \
	done

push:
	@set -eu; \
	for service in $(SERVICES); do \
		case "$$service" in \
			app) image="$(APP_NAME)";; \
			agent) image="$(APP_NAME)-agent";; \
			preview-runtime) image="$(APP_NAME)-preview-runtime";; \
			*) echo "Unknown service: $$service" >&2; exit 2;; \
		esac; \
		ref="$(IMAGE_ROOT)/$$image:$(TAG)"; \
		latest_ref="$(IMAGE_ROOT)/$$image:$(LATEST_TAG)"; \
		push_refs="$$ref"; \
		if [ "$(PUSH_LATEST)" = "1" ] && [ "$(TAG)" != "$(LATEST_TAG)" ]; then \
			echo "==> Tagging $$latest_ref"; \
			$(DOCKER) tag "$$ref" "$$latest_ref"; \
			push_refs="$$push_refs $$latest_ref"; \
		fi; \
		for push_ref in $$push_refs; do \
			echo "==> Pushing $$push_ref"; \
			$(DOCKER) push "$$push_ref"; \
		done; \
	done
	@$(MAKE) --no-print-directory digests

build-push: build push

pin:
	@set -eu; \
	for service in $(ALL_SERVICES); do \
		case "$$service" in \
			app) image="$(APP_NAME)";; \
			agent) image="$(APP_NAME)-agent";; \
			preview-runtime) image="$(APP_NAME)-preview-runtime";; \
		esac; \
		ref="$(IMAGE_ROOT)/$$image:$(TAG)"; \
		digest="$$($(BUILDX) imagetools inspect "$$ref" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"; \
		if [ -z "$$digest" ]; then \
			echo "$$ref が見つかりません。先に make push してください。" >&2; \
			exit 1; \
		fi; \
		python3 setup/manifest/pin.py --repository "$$service" "$$digest"; \
	done

# Per-service shortcuts, for example: make build-push-agent.
define SERVICE_RULES
build-$(1):
	@$$(MAKE) build SERVICES=$(1)

push-$(1):
	@$$(MAKE) push SERVICES=$(1)

build-push-$(1):
	@$$(MAKE) build-push SERVICES=$(1)
endef

$(foreach service,$(SERVICE_SHORTCUTS),$(eval $(call SERVICE_RULES,$(service))))

# ── Docker Composeのお試し版（compose.yaml）─────────────────────────
# k3sが無くても、1人用・ログインなしで生成・プレビュー・公開を試せる。
# make は薄い別名。docker compose up -d --build だけでも同じように起動する。
compose-up:
	$(DOCKER) compose up --detach --build
	@$(DOCKER) compose port app 8080 | sed 's/.*:/http:\/\/localhost:/'

# プレビューは管理アプリが docker run で起動するので、composeの管理外。先に片付ける。
compose-down:
	-@$(DOCKER) rm --force $$($(DOCKER) ps --all --quiet --filter label=koyorina-preview=1) 2>/dev/null
	$(DOCKER) compose down

compose-logs:
	$(DOCKER) compose logs --follow app codex-controller agent
