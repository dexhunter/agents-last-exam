"""OpenClawCliDeployer with an episode-local OpenClaw gateway.

Official npm package by default, with a native CUA plugin (not MCP).

Config files written: ``openclaw.json``, ``auth-profiles.json``, and
``workspace-state.json``.

Output: JSON envelope on stdout or stderr, plus the agent session transcript.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import ClassVar

from ale_run.base_interface import (
    AgentRunResult,
    BaseAgentDeployer,
    ContentPart,
    ImageSource,
    Observation,
    StepMetrics,
    ToolCall,
    ToolResult,
    TrajectoryBuilder,
)

from .config import CUA_TOOL_NAMES, OpenClawCliConfig
from .runtime import install_cli
from .vision import (
    VisionUsageProxy,
    persist_transcript_images,
    read_image_model_usage,
    run_dir_for_artifacts,
    stage_transcript_file_images,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 2.0
_TERM_GRACE_S = 2.0
_FINAL_EXIT_GRACE_S = 10.0
_STATE_READ_TIMEOUT_S = 15.0
_SUBAGENT_DELIVERY_TIMEOUT_S = 60.0
_SUBAGENT_POLL_S = 1.0
_SUBAGENT_SETTLE_S = 3.0
_AGENT_ID = "main"
_PLUGIN_PROVIDER_IDS = frozenset({"anthropic", "openai", "openrouter", "zai"})
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _serialize_cua_tools(plugin_dir: Path) -> None:
    """Keep desktop actions in model-specified order within a tool batch."""
    entry = plugin_dir / "dist" / "index.cjs"
    original = entry.with_name("index-upstream.cjs")
    entry.rename(original)
    entry.write_text(
        '''const upstream = require("./index-upstream.cjs");
const register = upstream.default ?? upstream;
exports.default = function (api) {
  return register(new Proxy(api, {
    get(target, key, receiver) {
      if (key === "registerTool") {
        return (tool, options) => target.registerTool(
          {...tool, executionMode: "sequential"}, options
        );
      }
      return Reflect.get(target, key, receiver);
    }
  }));
};
''',
        encoding="utf-8",
    )


class OpenClawCliDeployer(BaseAgentDeployer):
    """Stdlib-only deployer for ``openclaw agent`` and its gateway."""

    default_executor: ClassVar[str] = "sandbox"
    supported_executors: ClassVar[frozenset[str]] = frozenset({"sandbox"})
    hot_artifacts: ClassVar[tuple[str, ...]] = ("stdout.log", "stderr.log", "gateway.log")

    @property
    def version(self) -> str:
        return self.config.cli_version

    @property
    def _state_dir(self) -> Path:
        return getattr(self, "_episode_state_dir", Path.home() / ".openclaw")

    # =========================================================================
    # install
    # =========================================================================

    async def _install_from_tarball(self, tarball: str) -> None:
        from ale_run.agents._bootstrap import ensure_npm
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            npm = await ensure_npm()
        home = os.path.expanduser("~")
        prefix = os.path.join(home, ".local")
        env = {**os.environ, "npm_config_cache": os.path.join(home, ".npm-ale")}
        proc = await asyncio.to_thread(
            subprocess.run,
            [npm, "install", "-g", "--prefix", prefix, tarball],
            capture_output=True, text=True, timeout=300, env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"npm install -g {tarball} failed "
                f"(rc={proc.returncode}): {(proc.stderr or '')[:500]}"
            )
        # npm drops the openclaw shim in <prefix>/bin (Linux) or directly in
        # <prefix> (Windows). Put both on PATH so shutil.which finds it.
        for bin_dir in (prefix, os.path.join(prefix, "bin")):
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        logger.info("openclaw_cli: installed from tarball — %s",
                     (proc.stdout or "").strip()[-200:])

        # Verify dist integrity. A truncated tarball download or a partial
        # npm extraction silently drops esbuild dynamic-import chunks; the CLI
        # then dies at runtime with ERR_MODULE_NOT_FOUND for the missing
        # provider-runtime / transcript-resolve chunks. Fail fast here with a
        # clear cause rather than letting a broken install reach launch.
        # Global node_modules layout differs per OS: <prefix>/lib/node_modules
        # on Linux, <prefix>/node_modules on Windows.
        for nm in (
            Path(prefix) / "lib" / "node_modules" / "openclaw" / "dist",
            Path(prefix) / "node_modules" / "openclaw" / "dist",
        ):
            if nm.is_dir():
                dist_dir = nm
                break
        else:
            dist_dir = Path(prefix) / "lib" / "node_modules" / "openclaw" / "dist"
        chunk_count = len(list(dist_dir.glob("*.js"))) if dist_dir.is_dir() else 0
        if chunk_count < 1000:
            raise RuntimeError(
                f"openclaw tarball install looks incomplete: only {chunk_count} "
                f"dist/*.js chunks under {dist_dir} (expected >2000). The "
                f"tarball download was likely truncated — re-run or bake "
                f"openclaw into the image."
            )

    async def _download_tarball(self, url: str) -> str:
        """Download fork tarball from a URL, return local path."""
        home = os.path.expanduser("~")
        dest = Path(home) / ".ale-openclaw-fork.tgz"
        # --retry guards against transient drops that produce a truncated
        # tarball (the historic root cause of ERR_MODULE_NOT_FOUND at launch:
        # npm extracts the partial tgz, silently dropping dist chunks).
        proc = await asyncio.to_thread(
            subprocess.run,
            ["curl", "-fSL", "--retry", "3", "--retry-all-errors",
             "-o", str(dest), url],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"openclaw tarball download failed (rc={proc.returncode}): "
                f"{(proc.stderr or '')[:500]}"
            )
        size = dest.stat().st_size if dest.exists() else 0
        if size < 1_000_000:
            raise RuntimeError(
                f"openclaw tarball download truncated: {size} bytes at {dest} "
                f"(expected >1MB)"
            )
        logger.info("openclaw_cli: tarball downloaded to %s (%d bytes)", dest, size)
        return str(dest)

    async def _clone_cua_plugin(self, repo_url: str, branch: str) -> str:
        """Clone the CUA plugin source from the fork repo."""
        home = os.path.expanduser("~")
        clone_dir = Path(home) / ".ale-openclaw-repo"
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        proc = await asyncio.to_thread(
            subprocess.run,
            ["git", "clone", "--depth", "1", "-b", branch,
             "--filter=blob:none", "--sparse", repo_url, str(clone_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"openclaw repo clone failed (rc={proc.returncode}): "
                f"{(proc.stderr or '')[:500]}"
            )
        proc2 = await asyncio.to_thread(
            subprocess.run,
            ["git", "sparse-checkout", "set", "cua-plugin"],
            capture_output=True, text=True, timeout=30,
            cwd=str(clone_dir),
        )
        if proc2.returncode != 0:
            raise RuntimeError(
                f"sparse-checkout failed (rc={proc2.returncode}): "
                f"{(proc2.stderr or '')[:500]}"
            )
        plugin_path = str(clone_dir / "cua-plugin")
        logger.info("openclaw_cli: CUA plugin cloned to %s", plugin_path)
        return plugin_path

    async def _build_cua_plugin(self, plugin_src: str) -> None:
        """Build CUA plugin from source and install to ~/.openclaw/extensions/cua/."""
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            from ale_run.agents._bootstrap import ensure_npm
            npm = await ensure_npm()

        home = os.path.expanduser("~")
        build_dir = Path(home) / ".ale-cua-plugin-build"
        if build_dir.exists():
            shutil.rmtree(build_dir)
        shutil.copytree(plugin_src, str(build_dir))

        env = {**os.environ, "npm_config_cache": f"{home}/.npm-ale"}
        for step_name, step_cmd in [
            ("npm install", [npm, "install", "--no-audit", "--no-fund"]),
            ("npm run build", [npm, "run", "build"]),
        ]:
            proc = await asyncio.to_thread(
                subprocess.run, step_cmd,
                capture_output=True, text=True, timeout=120,
                cwd=str(build_dir), env=env,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"CUA plugin {step_name} failed (rc={proc.returncode}): "
                    f"{(proc.stderr or '')[:500]}"
                )

        install_dir = Path(home) / ".openclaw" / "extensions" / "cua"
        install_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("package.json", "openclaw.plugin.json"):
            src = build_dir / fname
            if src.exists():
                shutil.copy2(str(src), str(install_dir / fname))
        dist_src = build_dir / "dist" / "index.cjs"
        if dist_src.exists():
            (install_dir / "dist").mkdir(exist_ok=True)
            shutil.copy2(str(dist_src), str(install_dir / "dist" / "index.cjs"))
        if not (install_dir / "dist" / "index.cjs").is_file():
            raise RuntimeError("openclaw_cli: CUA plugin build did not produce its entrypoint")
        manifest_path = install_dir / "openclaw.plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("contracts", {})["tools"] = list(CUA_TOOL_NAMES)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        package_path = install_dir / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        # esbuild bundles the plugin's dependencies into index.cjs.
        package.pop("dependencies", None)
        package.pop("devDependencies", None)
        package_path.write_text(json.dumps(package, indent=2), encoding="utf-8")
        probe = await asyncio.to_thread(
            subprocess.run, [self._node_path, str(install_dir / "dist" / "index.cjs"), "--selftest"],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError("openclaw_cli: CUA plugin self-test failed: " + probe.stderr[-500:])
        logger.info("openclaw_cli: CUA plugin installed at %s", install_dir)

    @staticmethod
    def _route_model(model: str, provider: str) -> str:
        """Prefix a model ref with the auth provider so openclaw routes
        it correctly.  Without the prefix, ``anthropic/claude-...`` is sent
        to the ``anthropic`` provider directly, which has no configured key.
        Models already prefixed with a known provider are left untouched.
        """
        if model.split("/", 1)[0] == provider:
            return model
        return f"{provider}/{model}"

    @staticmethod
    def _direct_provider_for_model(model: str) -> str:
        """Map a model ref to its native (direct) provider id.

        Used only when ``provider == "direct"``: openclaw's upstream has
        first-class ``openai`` and ``anthropic`` providers, so a direct run
        routes to whichever vendor owns the model. The vendor is taken from
        an explicit ``<vendor>/`` prefix when present, otherwise inferred
        from the bare model name.
        """
        head, _, _ = model.partition("/")
        if head in ("openai", "anthropic"):
            return head
        name = model.rsplit("/", 1)[-1].lower()
        if name.startswith("claude"):
            return "anthropic"
        if name.startswith(("gpt", "o1", "o3", "o4")):
            return "openai"
        raise RuntimeError(
            f"openclaw_cli: provider=direct cannot infer a native provider "
            f"for model {model!r}. Use an OpenAI (gpt-*) or Anthropic "
            f"(claude-*) model, or prefix it with 'openai/' or 'anthropic/'."
        )

    def _resolve_route(
        self,
        *,
        model: str,
        routing_provider: str,
        api_key_override: str | None,
        provider_id: str | None = None,
        api_key_env: str | None = None,
        purpose: str,
    ) -> tuple[str, str]:
        """Return ``(provider, api_key)`` for one configured model route.

        Explicit, provider-driven (not key-presence inference):
          - ``openrouter`` → OPENROUTER_API_KEY.
          - ``direct`` → openai/anthropic native provider chosen by the
            model's vendor, keyed by OPENAI_API_KEY / ANTHROPIC_API_KEY.
          - ``zai`` → ZAI_API_KEY, Z_AI_API_KEY, or GLM_API_KEY.
          - ``custom`` → caller-supplied provider id and key env var.
        Missing the required key for the chosen provider is a hard error.
        """
        env = self.executor.env or {}

        def _key(name: str) -> str:
            return env.get(name) or os.environ.get(name, "")

        if routing_provider == "openrouter":
            # A literal override (e.g. api_key: ${env:ARK_API_KEY}) travels
            # with the serialized config and takes precedence, so it does not
            # require — or collide with — a real OPENROUTER_API_KEY env var.
            api_key = api_key_override or _key("OPENROUTER_API_KEY")
            if not api_key:
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=openrouter but neither "
                    "its config api_key nor OPENROUTER_API_KEY is set."
                )
            return "openrouter", api_key

        if routing_provider == "direct":
            provider = self._direct_provider_for_model(model)
            key_var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
            # A literal override (e.g. api_key: ${env:ARK_API_KEY}) takes
            # precedence — lets an OpenAI-compatible gateway (model prefixed
            # ``openai/...`` + base_url) authenticate without the native env var.
            api_key = api_key_override or _key(key_var)
            if not api_key:
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=direct resolved to "
                    f"{provider!r} for model {model!r} but neither its config "
                    f"api_key nor {key_var} is set."
                )
            return provider, api_key

        if routing_provider == "zai":
            api_key = (
                api_key_override
                or _key("ZAI_API_KEY")
                or _key("Z_AI_API_KEY")
                or _key("GLM_API_KEY")
            )
            if not api_key:
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=zai but neither its "
                    "config api_key, ZAI_API_KEY, Z_AI_API_KEY, nor "
                    "GLM_API_KEY is set."
                )
            return "zai", api_key

        if routing_provider == "custom":
            if not provider_id:
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=custom requires provider_id."
                )
            if not api_key_env or not _ENV_NAME_RE.fullmatch(api_key_env):
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=custom requires a valid "
                    "api_key_env environment variable name."
                )
            api_key = api_key_override or _key(api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"openclaw_cli: {purpose} provider=custom but neither its "
                    f"config api_key nor {api_key_env} is set."
                )
            return provider_id, api_key

        raise RuntimeError(
            f"openclaw_cli: unknown {purpose} provider {routing_provider!r} "
            "(expected 'openrouter', 'direct', 'zai', or 'custom')"
        )

    def _start_vision_usage_proxy(
        self,
        cfg: OpenClawCliConfig,
        work_dir: Path,
    ) -> None:
        if not cfg.vision_model:
            return
        routing_provider = cfg.vision_provider or cfg.provider
        try:
            resolved_provider = self._direct_provider_for_model(cfg.vision_model)
        except RuntimeError:
            return
        if routing_provider != "direct" or resolved_provider != "openai":
            return
        primary_route_model = cfg.model_id or cfg.model
        if (
            cfg.provider == "direct"
            and self._direct_provider_for_model(primary_route_model) == "openai"
        ) or (
            cfg.provider == "custom"
            and cfg.provider_id == "openai"
        ):
            logger.info(
                "openclaw_cli: image usage capture skipped because the primary "
                "and vision routes share the OpenAI provider"
            )
            return

        upstream_url = cfg.vision_base_url or "https://api.openai.com/v1"
        try:
            proxy = VisionUsageProxy(
                upstream_url=upstream_url,
                usage_log=work_dir / "vision-usage.jsonl",
                provider="openai",
                model=cfg.vision_model.split("/", 1)[-1],
            )
        except ValueError:
            logger.warning(
                "openclaw_cli: vision usage proxy skipped for unsupported "
                "upstream URL %s",
                upstream_url,
            )
            return
        proxy.start()
        self._vision_usage_proxy = proxy
        self._vision_usage_proxy_provider = "openai"
        self._vision_usage_proxy_url = proxy.base_url
        logger.info(
            "openclaw_cli: vision usage proxy listening at %s",
            self._vision_usage_proxy_url,
        )

    def _start_primary_compat_proxy(
        self,
        cfg: OpenClawCliConfig,
        work_dir: Path,
    ) -> None:
        if (
            cfg.provider != "custom"
            or not cfg.sanitize_empty_text_blocks
            or not cfg.base_url
            or not cfg.provider_id
        ):
            return
        proxy = VisionUsageProxy(
            upstream_url=cfg.base_url,
            usage_log=None,
            provider=cfg.provider_id,
            model=cfg.model_id or cfg.model,
            sanitize_empty_text_blocks=True,
        )
        proxy.start()
        self._primary_compat_proxy = proxy
        self._primary_compat_proxy_provider = cfg.provider_id
        self._primary_compat_proxy_url = proxy.base_url
        logger.info(
            "openclaw_cli: primary compatibility proxy listening at %s",
            self._primary_compat_proxy_url,
        )

    def _stop_vision_usage_proxy(self) -> None:
        for attribute in ("_vision_usage_proxy", "_primary_compat_proxy"):
            proxy = getattr(self, attribute, None)
            if proxy is not None:
                proxy.stop()
                setattr(self, attribute, None)

    def _write_config(self, cfg: OpenClawCliConfig) -> None:
        """Write the OpenClaw configuration, credentials, and workspace state."""
        oc_home = self._state_dir
        oc_home.mkdir(parents=True, exist_ok=True)

        # Explicit, provider-driven routing (not key-presence inference).
        # The model ref is prefixed with the resolved provider so openclaw
        # routes the request through it: "openrouter/<model>" for the
        # OpenRouter gateway, or the native "openai/..." / "anthropic/..."
        # provider for a direct run.
        primary_route_model = cfg.model_id or cfg.model
        provider, api_key = self._resolve_route(
            model=primary_route_model,
            routing_provider=cfg.provider,
            api_key_override=cfg.api_key,
            provider_id=cfg.provider_id,
            api_key_env=cfg.api_key_env,
            purpose="primary",
        )
        vision_provider: str | None = None
        vision_api_key: str | None = None
        vision_base_url: str | None = None
        if cfg.vision_model:
            vision_routing_provider = cfg.vision_provider or cfg.provider
            vision_base_url = (
                cfg.vision_base_url
                if cfg.vision_provider is not None
                else cfg.vision_base_url or cfg.base_url
            )
            vision_key_override = cfg.vision_api_key
            if cfg.vision_provider is None and vision_key_override is None:
                vision_key_override = cfg.api_key
            vision_provider, vision_api_key = self._resolve_route(
                model=cfg.vision_model,
                routing_provider=vision_routing_provider,
                api_key_override=vision_key_override,
                provider_id=cfg.provider_id,
                api_key_env=cfg.api_key_env,
                purpose="vision",
            )
            proxy_url = getattr(self, "_vision_usage_proxy_url", None)
            if proxy_url:
                vision_base_url = proxy_url
            primary_proxy_url = getattr(self, "_primary_compat_proxy_url", None)
            if (
                primary_proxy_url
                and vision_provider
                == getattr(self, "_primary_compat_proxy_provider", None)
                and vision_base_url == cfg.base_url
            ):
                vision_base_url = primary_proxy_url

        # --- openclaw.json ---
        primary_model = self._route_model(primary_route_model, provider)
        # The resolved provider's plugin must be enabled for its auth
        # profile to load (e.g. "anthropic" is not in the default allow set).
        plugins_allow = list(cfg.plugins_allow)
        if plugins_allow:
            for required_provider in (provider, vision_provider):
                if (
                    required_provider in _PLUGIN_PROVIDER_IDS
                    and required_provider not in plugins_allow
                ):
                    plugins_allow.append(required_provider)
        tools_also_allow = list(CUA_TOOL_NAMES)
        agent_defaults: dict = {
            "model": {"primary": primary_model},
            "timeoutSeconds": int(cfg.agent_timeout_s),
            # Keep doctor from migrating the per-model settings into a legacy
            # model allowlist. An empty policy preserves upstream's default.
            "modelPolicy": {"allow": []},
            "models": {
                primary_model: (
                    {"params": cfg.model_params}
                    if cfg.model_params is not None
                    else {}
                ),
            },
        }
        if cfg.heartbeat_every:
            heartbeat = "0m" if cfg.heartbeat_every.lower() == "never" else cfg.heartbeat_every
            agent_defaults["heartbeat"] = {"every": heartbeat}
        oc_config = {
            "agents": {
                "defaults": agent_defaults,
            },
            "plugins": {
                # Point the native CUA plugin at this image's cua-server. The
                # plugin defaults to localhost:5000 (correct on GCE); ale-kasm
                # runs cua-server on 8000, so set it explicitly from the
                # executor's bridge URL. Key path: plugins.entries.<id>.config.
                "entries": {
                    "cua": {
                        "config": {
                            "serverUrl": self.executor.cua_bridge_url(),
                        },
                    },
                },
            },
            "tools": {
                "alsoAllow": tools_also_allow,
            },
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "auth": {"mode": "token"},
            },
        }
        if plugins_allow:
            oc_config["plugins"]["allow"] = plugins_allow
        if cfg.plugins_deny:
            oc_config["plugins"]["deny"] = list(cfg.plugins_deny)
        if cfg.tools_deny:
            oc_config["tools"]["deny"] = list(cfg.tools_deny)
        if cfg.vision_model:
            vision_model = self._route_model(cfg.vision_model, vision_provider)
            agent_defaults["imageModel"] = {"primary": vision_model}
            agent_defaults["models"].setdefault(vision_model, {})
        # Custom OpenAI-compatible endpoint for the resolved provider. openclaw
        # reads ``models.providers.<id>.baseUrl`` to override a provider's
        # built-in endpoint — used to point the (chat-completions) openrouter
        # path at Volcengine Ark's /api/v3 gateway. The provider config schema
        # also requires a non-empty ``models`` array declaring each usable model
        # id (bare, no provider prefix); openclaw surfaces them as
        # ``<provider>/<id>``. We register the primary (and vision) model ids.
        provider_catalogs: dict[str, dict] = {}
        route_catalog_specs = [
            (
                provider,
                getattr(self, "_primary_compat_proxy_url", None) or cfg.base_url,
                primary_route_model,
                cfg.model,
                cfg.provider_api if cfg.provider == "custom" else None,
            ),
            (
                vision_provider,
                vision_base_url,
                cfg.vision_model,
                cfg.vision_model,
                None,
            ),
        ]
        for (
            catalog_provider,
            catalog_base_url,
            catalog_model,
            catalog_name,
            catalog_api,
        ) in route_catalog_specs:
            if not catalog_provider or not catalog_base_url or not catalog_model:
                continue
            head, sep, tail = catalog_model.partition("/")
            catalog_id = tail if sep and head == catalog_provider else catalog_model
            existing = provider_catalogs.get(catalog_provider)
            if existing and existing["baseUrl"] != catalog_base_url:
                raise RuntimeError(
                    "openclaw_cli: primary and vision routes resolve to provider "
                    f"{catalog_provider!r} with different base URLs"
                )
            if existing is None:
                existing = {
                    "baseUrl": catalog_base_url,
                    "models": [],
                }
                if catalog_provider == "zai" or catalog_api:
                    existing["api"] = catalog_api or "openai-completions"
                provider_catalogs[catalog_provider] = existing
            if (
                catalog_provider
                == getattr(self, "_vision_usage_proxy_provider", None)
                or catalog_provider
                == getattr(self, "_primary_compat_proxy_provider", None)
            ):
                existing["request"] = {"allowPrivateNetwork": True}
            if not any(model["id"] == catalog_id for model in existing["models"]):
                model_entry = {
                    "id": catalog_id,
                    "name": catalog_name or catalog_id,
                }
                if (
                    cfg.provider == "custom"
                    and catalog_provider == provider
                    and cfg.model_input_types is not None
                ):
                    model_entry["input"] = list(cfg.model_input_types)
                if (
                    catalog_provider
                    == getattr(self, "_vision_usage_proxy_provider", None)
                    and catalog_model == cfg.vision_model
                ):
                    model_entry["input"] = ["text", "image"]
                if cfg.provider == "custom" and catalog_provider == provider:
                    model_entry["compat"] = {
                        "maxTokensField": "max_completion_tokens",
                        "supportsUsageInStreaming": cfg.supports_usage_in_streaming,
                    }
                existing["models"].append(model_entry)
        if provider_catalogs:
            oc_config["models"] = {"providers": provider_catalogs}
        (oc_home / "openclaw.json").write_text(
            json.dumps(oc_config, indent=2), encoding="utf-8",
        )

        # --- auth-profiles.json ---
        agent_dir = oc_home / "agents" / _AGENT_ID / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)

        auth_keys = {provider: api_key}
        if vision_provider and vision_api_key:
            existing_key = auth_keys.get(vision_provider)
            if existing_key is not None and existing_key != vision_api_key:
                raise RuntimeError(
                    "openclaw_cli: primary and vision routes resolve to provider "
                    f"{vision_provider!r} with different API keys"
                )
            auth_keys[vision_provider] = vision_api_key
        auth = {
            "profiles": {
                f"{auth_provider}:default": {
                    "provider": auth_provider,
                    "type": "api_key",
                    "key": auth_key,
                }
                for auth_provider, auth_key in auth_keys.items()
            },
            "lastGood": {
                auth_provider: f"{auth_provider}:default"
                for auth_provider in auth_keys
            },
        }
        (agent_dir / "auth-profiles.json").write_text(
            json.dumps(auth, indent=2), encoding="utf-8",
        )

        # --- workspace bootstrap completion (skip the interactive wizard) ---
        # OpenClaw's embedded agent reads its workspace at
        # ``~/.openclaw/workspace/`` and treats bootstrap as *pending* unless
        # ``<workspace>/.openclaw/workspace-state.json`` carries
        # ``setupCompletedAt`` AND ``<workspace>/BOOTSTRAP.md`` is gone. While
        # pending, the agent enters the interactive "who am I?" bootstrap
        # conversation (BOOTSTRAP.md) instead of the task; under ``--json`` it
        # buffers all output to the end, so on a headless VM it produces zero
        # stdout/stderr and hangs until the wall budget — observed as a silent
        # Windows runtime hang. (The runtime materializes this workspace lazily
        # on first run, so we also re-assert these markers in launch().)
        self._complete_workspace_bootstrap(oc_home)

        # --- .env ---
        env_file = oc_home / ".env"
        env_file.write_text("OPENCLAW_RAW_STREAM=0\n", encoding="utf-8")

        logger.info("openclaw_cli: config staged at %s", oc_home)

    def _complete_workspace_bootstrap(self, oc_home: Path | None = None) -> None:
        """Mark OpenClaw's workspace bootstrap complete so ``agent``
        skips the interactive wizard.

        Mirrors agenthle's verified Windows/Linux path. OpenClaw resolves its
        agent workspace to ``~/.openclaw/workspace/`` and considers bootstrap
        *pending* until ``<workspace>/.openclaw/workspace-state.json`` carries
        ``setupCompletedAt`` and ``<workspace>/BOOTSTRAP.md`` is removed. We
        also seed ``MEMORY.md`` + ``memory/{today,yesterday}.md`` (touch-only,
        never truncating) because the default workspace ``AGENTS.md`` tells the
        agent to read them at session start and the lazy writers that create
        them never fire in a single short benchmark run.

        Pure stdlib + filesystem (the deployer runs inside the sandbox), so a
        single implementation covers Linux and Windows. Idempotent.
        """
        if oc_home is None:
            oc_home = self._state_dir
        workspace = oc_home / "workspace"
        ws_state_dir = workspace / ".openclaw"
        ws_state_dir.mkdir(parents=True, exist_ok=True)

        state_path = ws_state_dir / "workspace-state.json"
        state: dict = {"version": 1}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {"version": 1}
        now = datetime.now(timezone.utc).isoformat()
        state.setdefault("bootstrapSeededAt", now)
        state["setupCompletedAt"] = now
        state_path.write_text(
            json.dumps(state, indent=2) + "\n", encoding="utf-8",
        )

        bootstrap_md = workspace / "BOOTSTRAP.md"
        if bootstrap_md.exists():
            try:
                bootstrap_md.unlink()
            except OSError:
                pass

        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for f in (
            workspace / "MEMORY.md",
            memory_dir / f"{today}.md",
            memory_dir / f"{yesterday}.md",
        ):
            try:
                f.touch(exist_ok=True)
            except OSError:
                pass
        logger.info(
            "openclaw_cli: workspace bootstrap marked complete at %s", workspace,
        )

    async def install(self) -> None:
        cfg: OpenClawCliConfig = self.config  # type: ignore[assignment]

        from ale_run.agents._bootstrap import ensure_node_npm
        from ale_run.agents.qwen_code.runtime import command

        node, npm = await ensure_node_npm()
        tarball = cfg.tarball_path
        if tarball and not Path(tarball).is_file():
            if not cfg.tarball_url:
                raise RuntimeError(f"openclaw_cli: tarball not found: {tarball}")
            tarball = await self._download_tarball(cfg.tarball_url)
        elif not tarball and cfg.tarball_url:
            tarball = await self._download_tarball(cfg.tarball_url)
        self._node_path, self._openclaw_path = await install_cli(
            node, npm, cfg.cli_version, tarball,
        )
        self.actual_cli_version = cfg.cli_version
        self.cli_source = "custom" if tarball else "official"
        home = os.path.expanduser("~")
        self._episode_state_dir = Path(home) / ".ale-openclaw" / "states" / str(uuid.uuid4())
        self._episode_state_dir.mkdir(mode=0o700, parents=True)
        self._gateway_token = secrets.token_urlsafe(32)

        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)

        # 2. Build CUA plugin (if source available and not already installed)
        home = os.path.expanduser("~")
        plugin_entry = Path(home) / ".openclaw" / "extensions" / "cua" / "dist" / "index.cjs"
        manifest_path = plugin_entry.parent.parent / "openclaw.plugin.json"
        package_path = plugin_entry.parent.parent / "package.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            package = json.loads(package_path.read_text(encoding="utf-8"))
            plugin_ready = (
                plugin_entry.is_file()
                and set(manifest.get("contracts", {}).get("tools", ())) == set(CUA_TOOL_NAMES)
                and not package.get("dependencies")
            )
        except (OSError, ValueError):
            plugin_ready = False
        if not plugin_ready:
            plugin_src = cfg.cua_plugin_path
            if not Path(plugin_src).is_dir() and cfg.cua_plugin_repo:
                logger.info("openclaw_cli: CUA plugin not at %s, cloning from %s",
                            plugin_src, cfg.cua_plugin_repo)
                plugin_src = await self._clone_cua_plugin(
                    cfg.cua_plugin_repo, cfg.cua_plugin_branch,
                )
            if Path(plugin_src).is_dir():
                logger.info("openclaw_cli: building CUA plugin from %s", plugin_src)
                await self._build_cua_plugin(plugin_src)
            else:
                raise RuntimeError(f"openclaw_cli: CUA plugin source not found at {plugin_src}")
        else:
            logger.info("openclaw_cli: CUA plugin already installed")

        episode_plugin = self._state_dir / "extensions" / "cua"
        shutil.copytree(plugin_entry.parent.parent, episode_plugin)
        _serialize_cua_tools(episode_plugin)

        # 3. Start any provider compatibility proxy before writing config.
        self._start_primary_compat_proxy(cfg, wd)

        # 4. Route the direct OpenAI vision model through an ALE-owned
        # loopback proxy that records response usage only.
        self._start_vision_usage_proxy(cfg, wd)

        try:
            # 5. Write config files
            self._write_config(cfg)

            env = self._build_env(cfg, self._state_dir / ".env")
            doctor = await command(
                self._launch_prefix() + ["doctor", "--fix", "--non-interactive"],
                env=env, timeout=600,
            )
            if doctor.returncode != 0:
                raise RuntimeError("openclaw_cli: credential migration failed: " + doctor.stderr[-1000:])
            inspection = await command(
                self._launch_prefix() + ["plugins", "inspect", "cua", "--runtime", "--json"],
                env=env, timeout=180,
            )
            try:
                plugin = json.loads(inspection.stdout)["plugin"]
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError("openclaw_cli: could not inspect CUA plugin") from exc
            if (inspection.returncode != 0 or plugin.get("status") != "loaded"
                    or set(plugin.get("toolNames", ())) != set(CUA_TOOL_NAMES)):
                raise RuntimeError("openclaw_cli: CUA plugin failed to load all required tools: "
                                   + str(plugin.get("error") or plugin.get("toolNames")))
            logger.warning(
                "openclaw_cli: llm_idle_timeout_s is unsupported by official OpenClaw %s; "
                "the upstream model timeout policy applies", cfg.cli_version,
            )
            # 6. Pre-warm the bundled-plugin runtime-deps mirror.
            await self._prewarm_plugin_runtime_mirror(cfg)
        except BaseException:
            self._stop_vision_usage_proxy()
            raise

    async def _prewarm_plugin_runtime_mirror(self, cfg: OpenClawCliConfig) -> None:
        """Force OpenClaw to build its bundled-plugin runtime-deps mirror now,
        during the untimed install, rather than on the first (timed) agent turn.

        When ``openclaw`` is installed to a global npm prefix it cannot write
        runtime deps in place, so on first plugin-load it mirrors the bundled
        plugins' dist tree (~60 plugins, thousands of files) into
        ``~/.openclaw/plugin-runtime-deps/.../dist/extensions/`` under a
        filesystem lock. On Windows this recursive copy takes minutes; if it
        runs inside ``agent`` it produces no stdout/stderr until done
        and blows the agent's wall budget — the silent runtime hang. The mirror
        is built once and reused (existing targets are skipped), so a cheap
        ``models list`` here (which loads the same provider plugins) pays the
        cost up front and leaves every subsequent ``launch()`` fast.

        Best-effort: a failure here is non-fatal — the agent turn would just
        pay (or re-pay) the mirror cost itself.
        """
        argv = self._launch_prefix() + ["models", "list"]
        env = self._build_env(cfg, self._state_dir / ".env")
        try:
            t0 = time.monotonic()
            proc = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True, text=True, env=env,
                timeout=600,
                cwd=str(Path(self.executor.work_dir)),
            )
            logger.info(
                "openclaw_cli: plugin runtime mirror pre-warmed in %.0fs (rc=%s)",
                time.monotonic() - t0, proc.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "openclaw_cli: plugin runtime mirror pre-warm timed out after 600s; "
                "the first agent turn may pay the remaining mirror cost"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "openclaw_cli: plugin runtime mirror pre-warm failed (%s); "
                "the first agent turn will build the mirror itself", exc,
            )

    # =========================================================================
    # launch
    # =========================================================================

    async def _start_gateway(self, env: dict[str, str], wd: Path, tree):
        from ale_run.agents.qwen_code.runtime import assign_job, command, stop_process

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config_path = self._state_dir / "openclaw.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["gateway"]["port"] = port
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        env["OPENCLAW_GATEWAY_TOKEN"] = self._gateway_token
        env["OPENCLAW_GATEWAY_PORT"] = str(port)
        with (wd / "gateway.log").open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                *self._launch_prefix(), "gateway", "run", "--bind", "loopback",
                "--port", str(port), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(wd),
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        job = tree
        try:
            if os.name == "nt":
                job = assign_job(proc.pid)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if proc.returncode is not None:
                    raise RuntimeError(f"OpenClaw gateway exited with code {proc.returncode}")
                try:
                    check = await command(
                        self._launch_prefix() + ["gateway", "call", "health", "--json", "--timeout", "2000"],
                        env=env, timeout=6,
                    )
                    if check.returncode == 0:
                        return proc, job
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1)
            raise RuntimeError("OpenClaw gateway did not become ready within 90 seconds")
        except BaseException:
            await stop_process(proc, job, _TERM_GRACE_S)
            raise

    async def _wait_for_subagents(self, envelope: dict | None, gateway=None) -> None:
        expected = {
            item["runId"]: item
            for item in (envelope or {}).get("acceptedSessionSpawns", [])
            if isinstance(item, dict) and isinstance(item.get("runId"), str)
        }
        state_db = self._state_dir / "state" / "openclaw.sqlite"
        agent_db = self._state_dir / "agents" / _AGENT_ID / "agent" / "openclaw-agent.sqlite"
        settled_at = None
        read_error_since = None
        missing_since = time.monotonic()
        archived_since: dict[str, float] = {}
        delivered: set[str] = set()
        while True:
            if gateway is not None and gateway.returncode is not None:
                raise RuntimeError("OpenClaw gateway exited while subagents were running")
            try:
                with sqlite3.connect(state_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                    rows = db.execute("SELECT run_id, payload_json FROM subagent_runs").fetchall()
                runs = {run_id: json.loads(payload) for run_id, payload in rows}
                missing = expected.keys() - runs.keys()
                if not runs and not missing:
                    return
                unfinished = any(
                    run.get("execution", {}).get("status") != "terminal"
                    or run.get("delivery", {}).get("status") not in {
                        "delivered", "not_required", "discarded", "failed",
                    }
                    for run in runs.values()
                )
                with sqlite3.connect(agent_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as db:
                    archives = set()
                    if missing and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_transcript_archives'"
                    ).fetchone():
                        archives = {row[0] for row in db.execute(
                            "SELECT session_key FROM session_transcript_archives WHERE reason='deleted'"
                        )}
                    absent = {
                        run_id for run_id in missing
                        if expected[run_id].get("childSessionKey") not in archives
                    }
                    if absent:
                        if time.monotonic() - missing_since > _STATE_READ_TIMEOUT_S:
                            raise RuntimeError("OpenClaw did not persist an accepted subagent run")
                        await asyncio.sleep(_SUBAGENT_POLL_S)
                        continue
                    active = db.execute(
                        "SELECT COUNT(*) FROM session_nodes WHERE status IN ('running', 'active')"
                    ).fetchone()[0]
                    pending_delivery = False
                    for run_id in missing:
                        if expected[run_id].get("expectsCompletionMessage") is False:
                            continue
                        archived_since.setdefault(run_id, time.monotonic())
                        if run_id not in delivered:
                            pattern = f"%{run_id}%"
                            delivered_row = db.execute(
                                "SELECT 1 FROM session_nodes WHERE "
                                "json_extract(entry_json, '$.lastRunId') LIKE ? OR "
                                "json_extract(entry_json, '$.activeWriterRunId') LIKE ? LIMIT 1",
                                (pattern, pattern),
                            ).fetchone() or db.execute(
                                "SELECT 1 FROM transcript_events WHERE event_json IS NOT NULL AND "
                                "json_extract(event_json, '$.message.role') = 'assistant' AND "
                                "json_extract(event_json, '$.message.__openclaw.runId') LIKE ? LIMIT 1",
                                (pattern,),
                            ).fetchone()
                            if delivered_row:
                                delivered.add(run_id)
                        if run_id not in delivered:
                            if not active and time.monotonic() - archived_since[run_id] > _SUBAGENT_DELIVERY_TIMEOUT_S:
                                raise RuntimeError("OpenClaw archived a subagent without a completion delivery")
                            pending_delivery = True
                read_error_since = None
            except (sqlite3.Error, json.JSONDecodeError) as exc:
                if read_error_since is None:
                    read_error_since = time.monotonic()
                elif time.monotonic() - read_error_since >= _STATE_READ_TIMEOUT_S:
                    raise RuntimeError("OpenClaw subagent state could not be read") from exc
                await asyncio.sleep(_SUBAGENT_POLL_S)
                continue
            if unfinished or active or pending_delivery:
                settled_at = None
            elif settled_at is None:
                settled_at = time.monotonic()
            elif time.monotonic() - settled_at >= _SUBAGENT_SETTLE_S:
                return
            await asyncio.sleep(_SUBAGENT_POLL_S)

    async def _export_child_transcripts(self, wd: Path, envelope: dict | None = None) -> None:
        database = self._state_dir / "agents" / _AGENT_ID / "agent" / "openclaw-agent.sqlite"
        expected = {
            item.get("childSessionKey")
            for item in (envelope or {}).get("acceptedSessionSpawns", [])
            if isinstance(item, dict) and isinstance(item.get("childSessionKey"), str)
        }
        children_dir = wd / "subagents"
        try:
            with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
                rows = db.execute(
                    "SELECT session_key, current_session_id, parent_session_key, status, entry_json "
                    "FROM session_nodes WHERE parent_session_key IS NOT NULL ORDER BY created_at"
                ).fetchall()
                archives = []
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_transcript_archives'"
                ).fetchone():
                    archives = db.execute(
                        "SELECT session_key, session_id FROM session_transcript_archives "
                        "WHERE reason='deleted' AND session_key LIKE ? ORDER BY created_at",
                        (f"agent:{_AGENT_ID}:subagent:%",),
                    ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("openclaw_cli: could not list child sessions: %s", exc)
            children_dir.mkdir(parents=True, exist_ok=True)
            (children_dir / "index.json").write_text(
                json.dumps({"error": "child session index unavailable"}), encoding="utf-8",
            )
            return
        known = {row[0] for row in rows}
        for session_key, session_id in archives:
            if session_key not in known:
                rows.append((session_key, session_id, None, "archived", "{}"))
                known.add(session_key)
        if not rows and not expected:
            return
        children_dir.mkdir(parents=True, exist_ok=True)
        children = []
        for session_key, session_id, parent_key, status, entry_json in rows:
            try:
                session_id = str(uuid.UUID(session_id))
                entry = json.loads(entry_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            destination = children_dir / f"{session_id}.jsonl"
            await self._export_session_transcript(session_id, destination)
            if destination.exists():
                try:
                    stage_transcript_file_images(destination, children_dir, self._state_dir / "workspace")
                except Exception as exc:
                    logger.warning("openclaw_cli: failed to stage child transcript images: %s", exc)
            children.append({
                "session_id": session_id, "session_key": session_key,
                "parent_session_key": parent_key, "status": status,
                "model": entry.get("model"), "provider": entry.get("modelProvider"),
                "transcript": destination.name if destination.exists() else None,
            })
        for key in sorted(expected - {record["session_key"] for record in children}):
            children.append({"session_key": key, "transcript": None})
        by_key = {record["session_key"]: record for record in children}
        main_key = ((envelope or {}).get("meta", {}).get("agentMeta", {}).get("sessionFile"))
        sources = [(main_key, wd / "transcript.jsonl")]
        sources.extend((record["session_key"], children_dir / record["transcript"])
                       for record in children if record.get("transcript"))
        for parent_key, transcript in sources:
            if not parent_key or not transcript.is_file():
                continue
            for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    message = json.loads(line).get("message", {})
                except (ValueError, AttributeError):
                    continue
                details = message.get("details") if isinstance(message, dict) else None
                if isinstance(details, dict) and isinstance(details.get("result"), dict):
                    details = details["result"].get("details")
                if isinstance(details, dict) and details.get("status") == "accepted":
                    child = by_key.get(details.get("childSessionKey"))
                    if child is not None:
                        child["parent_session_key"] = parent_key
        (children_dir / "index.json").write_text(json.dumps(children, indent=2), encoding="utf-8")

    async def launch(self, prompt: str) -> AgentRunResult:
        cfg: OpenClawCliConfig = self.config  # type: ignore[assignment]
        wd = Path(self.executor.work_dir)
        wd.mkdir(parents=True, exist_ok=True)

        # Re-assert workspace bootstrap completion: OpenClaw's runtime
        # materializes ~/.openclaw/workspace/ (with a fresh BOOTSTRAP.md) lazily
        # on first run, so the markers written during install() may have been
        # superseded before we launch the turn.
        self._complete_workspace_bootstrap()

        prompt_file = wd / "prompt.txt"
        stdout_log = wd / "stdout.log"
        stderr_log = wd / "stderr.log"
        pid_file = wd / "openclaw.pid"

        for f in (stdout_log, stderr_log, pid_file):
            if f.exists():
                try:
                    f.unlink()
                except OSError:
                    pass

        prompt_file.write_text(prompt, encoding="utf-8")

        env_file = self._state_dir / ".env"
        session_id = str(uuid.uuid4())
        argv = self._launch_prefix() + [
            "agent",
            "--agent", _AGENT_ID,
            "--session-id", session_id,
            "--message-file", str(prompt_file),
            "--json",
            "--timeout", str(int(cfg.agent_timeout_s)),
            "--thinking", cfg.thinking,
        ]
        env = self._build_env(cfg, env_file)

        from ale_run.agents.qwen_code.runtime import assign_job, stop_process

        tree = None
        if os.name != "nt":
            from ale_run.executors._sandbox_entry import _LinuxProcessTree
            tree = _LinuxProcessTree()
        t0 = time.monotonic()
        gateway = gateway_job = proc = None
        try:
            gateway, gateway_job = await self._start_gateway(env, wd, tree)
            with open(stdout_log, "wb") as fout, open(stderr_log, "wb") as ferr:
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdin=subprocess.DEVNULL, stdout=fout, stderr=ferr,
                    env=env, cwd=str(wd), start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            pid_file.write_text(str(proc.pid), encoding="ascii")
            logger.info("openclaw_cli: spawned pid=%s", proc.pid)
            result_envelope: dict | None = None
            envelope_time: float | None = None
            cli_job = None
            try:
                if os.name == "nt":
                    cli_job = assign_job(proc.pid)
                while proc.returncode is None:
                    if gateway is not None and gateway.returncode is not None:
                        raise RuntimeError("OpenClaw gateway exited during the agent run")
                    candidate = _result_from_logs(stdout_log, stderr_log)
                    if candidate is not None and envelope_time is None:
                        result_envelope = candidate
                        envelope_time = time.monotonic()
                    if envelope_time is not None and time.monotonic() - envelope_time >= _FINAL_EXIT_GRACE_S:
                        break
                    await asyncio.sleep(_POLL_INTERVAL_S)
            finally:
                exit_code = proc.returncode
                await stop_process(proc, cli_job, _TERM_GRACE_S)

            if result_envelope is None:
                result_envelope = _result_from_logs(stdout_log, stderr_log)
            envelope_error = _result_envelope_error(result_envelope)
            if envelope_error:
                status, error = "failed", envelope_error
            elif result_envelope is not None and exit_code in (0, None):
                status, error = "completed", None
                await self._wait_for_subagents(result_envelope, gateway)
            else:
                status = "failed"
                error = _diagnose_failure(stderr_log, exit_code)
                if result_envelope is None:
                    error += " | no valid OpenClaw result envelope"

            session_id = (
                (result_envelope or {}).get("meta", {}).get("agentMeta", {}).get("sessionId")
                or session_id
            )
            src = self._state_dir / "agents" / _AGENT_ID / "sessions" / f"{session_id}.jsonl"
            dst = wd / "transcript.jsonl"
            if src.exists():
                shutil.copy2(str(src), str(dst))
            else:
                await self._export_session_transcript(session_id, dst)
            if dst.exists():
                try:
                    stage_transcript_file_images(dst, wd, self._state_dir / "workspace")
                except Exception as exc:
                    logger.warning("openclaw_cli: failed to stage transcript images: %s", exc)
            await self._export_child_transcripts(wd, result_envelope)
            return AgentRunResult(
                status=status, pid=proc.pid, exit_code=exit_code,
                transcript_path=str(dst), stderr_path=str(stderr_log),
                duration_s=time.monotonic() - t0, error=error,
            )
        finally:
            try:
                if gateway is not None:
                    cleanup = asyncio.create_task(stop_process(gateway, gateway_job, _TERM_GRACE_S))
                    cancelled = False
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            cancelled = True
                    cleanup.result()
                    if cancelled:
                        raise asyncio.CancelledError
            finally:
                self._stop_vision_usage_proxy()

    async def _export_session_transcript(self, session_id: str, destination: Path) -> None:
        from ale_run.agents.qwen_code.runtime import command

        database = self._state_dir / "agents" / _AGENT_ID / "agent" / "openclaw-agent.sqlite"
        if not database.is_file():
            logger.warning("openclaw_cli: session database is missing")
            return
        script = r'''
const {DatabaseSync} = require("node:sqlite");
const {zstdDecompressSync} = require("node:zlib");
const db = new DatabaseSync(process.argv[1], {readOnly: true});
const rows = db.prepare("SELECT event_json, event_zstd FROM transcript_events WHERE session_id = ? ORDER BY seq").all(process.argv[2]);
if (rows.length) {
  for (const row of rows) {
    const text = row.event_json ?? zstdDecompressSync(row.event_zstd).toString("utf8");
    process.stdout.write(text + "\n");
  }
} else {
  const archive = db.prepare("SELECT encoding, archive_blob FROM session_transcript_archives WHERE session_id = ? ORDER BY generation DESC LIMIT 1").get(process.argv[2]);
  if (!archive || !archive.archive_blob) process.exit(2);
  const bytes = archive.encoding === "zstd" ? zstdDecompressSync(archive.archive_blob) : archive.archive_blob;
  if (archive.encoding !== "zstd" && archive.encoding !== "identity") process.exit(2);
  process.stdout.write(Buffer.from(bytes));
}
db.close();
'''
        proc = await command(
            [self._node_path, "-e", script, str(database), session_id],
            env={**os.environ, "NODE_NO_WARNINGS": "1"}, timeout=120,
        )
        if proc.returncode != 0:
            logger.warning("openclaw_cli: session export failed: %s", proc.stderr[-1000:])
            return
        destination.write_text(proc.stdout, encoding="utf-8")

    # =========================================================================
    # internals
    # =========================================================================

    def _launch_prefix(self) -> list[str]:
        """Argv prefix that invokes openclaw.

        On Linux the npm ``openclaw`` shim is launched directly. On Windows
        the ``openclaw.cmd`` shim mangles arg passing / loses its node lookup
        when spawned headless (see agenthle notes), so invoke
        ``node.exe <…>\\node_modules\\openclaw\\openclaw.mjs`` directly when
        the .mjs entry can be located; otherwise fall back to the shim.
        """
        if hasattr(self, "_node_path"):
            return [self._node_path, self._openclaw_path]
        if self.executor.sandbox.is_linux:
            return [self._openclaw_path]
        node = self.executor.sandbox.node
        if not node or not os.path.isfile(node):
            node = shutil.which("node") or shutil.which("node.exe") or "node"
        mjs = self._openclaw_mjs_entry()
        if mjs:
            return [node, mjs]
        return [self._openclaw_path]

    def _openclaw_mjs_entry(self) -> str | None:
        """Locate the openclaw.mjs bin entry under the global node_modules."""
        home = os.path.expanduser("~")
        candidates = [
            Path(home) / ".local" / "node_modules" / "openclaw" / "openclaw.mjs",
            Path(home) / ".local" / "lib" / "node_modules" / "openclaw" / "openclaw.mjs",
        ]
        # Derive from the resolved shim path's directory too (handles a
        # pre-baked install under a different prefix).
        shim = getattr(self, "_openclaw_path", None)
        if shim:
            shim_dir = Path(shim).parent
            candidates += [
                shim_dir / "node_modules" / "openclaw" / "openclaw.mjs",
                shim_dir / "lib" / "node_modules" / "openclaw" / "openclaw.mjs",
            ]
        for c in candidates:
            if c.is_file():
                return str(c)
        return None

    def _build_env(self, cfg: OpenClawCliConfig, env_file: Path) -> dict[str, str]:
        env = os.environ.copy()
        for k, v in (self.executor.env or {}).items():
            env[k] = v
        env["OPENCLAW_STATE_DIR"] = str(self._state_dir)
        env.pop("OPENCLAW_CONFIG_PATH", None)
        env.pop("OPENCLAW_GATEWAY_URL", None)
        env.pop("OPENCLAW_GATEWAY_PASSWORD", None)
        if token := getattr(self, "_gateway_token", None):
            env["OPENCLAW_GATEWAY_TOKEN"] = token
        else:
            env.pop("OPENCLAW_GATEWAY_TOKEN", None)
        env.pop("OPENCLAW_GATEWAY_PORT", None)
        env["NO_COLOR"] = "1"
        # For a direct run, drop the OpenRouter key so its presence (always
        # exported by the secrets sidecar) cannot make openclaw fall back to
        # the openrouter provider behind the explicitly-chosen direct one.
        uses_openrouter = cfg.provider == "openrouter" or (
            cfg.vision_model is not None
            and (cfg.vision_provider or cfg.provider) == "openrouter"
        )
        if not uses_openrouter:
            env.pop("OPENROUTER_API_KEY", None)
        # Source .env file values
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        return env

    @staticmethod
    def _extract_session_id(stderr_log: Path) -> str | None:
        """Extract session ID from the --json stderr envelope."""
        text = _read_text_tolerant(stderr_log)
        if not text:
            return None
        json_obj = _parse_stderr_json(text)
        if json_obj:
            meta = json_obj.get("meta", {})
            if not isinstance(meta, dict):
                return None
            agent_meta = meta.get("agentMeta", {})
            if isinstance(agent_meta, dict):
                return agent_meta.get("sessionId")
        return None

    # =========================================================================
    # parse_artifacts
    # =========================================================================

    @classmethod
    def _parse_child_trajectories(cls, work_dir: Path, builder: TrajectoryBuilder):
        children_dir = work_dir / "subagents"
        index = children_dir / "index.json"
        if not index.is_file():
            return [], False
        try:
            records = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("openclaw_cli: invalid subagent index")
            return [], True
        if not isinstance(records, list):
            logger.warning("openclaw_cli: subagent index unavailable")
            return [], True
        trajectories = {}
        incomplete = False
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get("session_key"), str)
                    or record["session_key"] in trajectories):
                incomplete = True
                continue
            child = TrajectoryBuilder(
                agent_name=builder.trajectory.agent.name,
                agent_version=builder.trajectory.agent.version,
                model=record.get("model") or builder.trajectory.agent.model,
                task_path=builder.trajectory.task_path,
                variant_index=builder.trajectory.variant_index,
            )
            child.trajectory.extra["openclaw_cli"] = {
                key: record.get(key) for key in ("session_id", "session_key", "parent_session_key", "status", "provider")
            }
            transcript = record.get("transcript")
            path = children_dir / Path(transcript).name if isinstance(transcript, str) else None
            if path is None or not path.is_file():
                incomplete = True
                child.add_step(source="system", message="OpenClaw subagent transcript unavailable")
            else:
                persist_transcript_images(path, run_dir_for_artifacts(work_dir))
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        incomplete = True
                        continue
                    message = event.get("message", {}) if isinstance(event, dict) else {}
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        if not record.get("model") and isinstance(message.get("model"), str) and message["model"]:
                            record["model"] = child.trajectory.agent.model = message["model"]
                        if not record.get("provider") and isinstance(message.get("provider"), str) and message["provider"]:
                            record["provider"] = child.trajectory.extra["openclaw_cli"]["provider"] = message["provider"]
                    cls._consume_session_event(event, child)
            result = child.finalize(reward=None)
            trajectories[record["session_key"]] = (record, result)
        for record, trajectory in trajectories.values():
            parent = trajectories.get(record.get("parent_session_key"))
            (parent[1] if parent else builder.trajectory).subagent_trajectories.append(trajectory)
        return [trajectory for _, trajectory in trajectories.values()], incomplete

    @classmethod
    def parse_artifacts(
        cls,
        *,
        work_dir: Path,
        config: OpenClawCliConfig,
        run_result: AgentRunResult,
        builder: TrajectoryBuilder,
    ) -> None:
        # 1. Parse session trajectory JSONL (if available)
        transcript_file = work_dir / "transcript.jsonl"
        if transcript_file.exists():
            persist_transcript_images(
                transcript_file,
                run_dir_for_artifacts(work_dir),
            )
            raw = transcript_file.read_text(encoding="utf-8", errors="replace")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cls._consume_session_event(event, builder)

        children, child_incomplete = cls._parse_child_trajectories(work_dir, builder)

        # 2. Parse the CLI result envelope.
        json_obj = _result_from_logs(work_dir / "stdout.log", work_dir / "stderr.log")
        usage: dict = {}
        if json_obj:
            builder.trajectory.extra.setdefault("openclaw_cli", {})["result_envelope"] = json_obj
            meta = json_obj.get("meta", {})
            agent_meta = meta.get("agentMeta", {})
            usage = agent_meta.get("usage", {}) or {}
            if usage:
                builder.trajectory.extra["openclaw_cli"]["usage"] = usage
        cost = usage.get("cost") if isinstance(usage, dict) else None
        total_cost = cost.get("total") if isinstance(cost, dict) else None
        if isinstance(total_cost, (int, float)) and not isinstance(total_cost, bool):
            if children:
                main_cost = sum(step.metrics.cost_usd or 0 for step in builder.trajectory.steps if step.metrics)
                total_cost = max(total_cost, main_cost) + sum(
                    child.final_metrics.total_cost_usd or 0 for child in children if child.final_metrics
                )
            builder.override_final_metrics(total_cost_usd=total_cost)
        elif any(step.metrics and step.metrics.cost_usd is None for step in builder.trajectory.steps):
            builder.mark_cost_unavailable()
        elif not any(step.metrics for step in builder.trajectory.steps):
            builder.mark_cost_unavailable()
        elif children:
            builder.override_final_metrics(total_cost_usd=sum(
                step.metrics.cost_usd or 0 for step in builder.trajectory.steps if step.metrics
            ) + sum(
                child.final_metrics.total_cost_usd or 0 for child in children if child.final_metrics
            ))
        if child_incomplete or any(
            not any(step.metrics for step in child.steps)
            or any(step.metrics and step.metrics.cost_usd is None for step in child.steps)
            for child in children
        ):
            builder.mark_cost_unavailable()

        image_usage = read_image_model_usage(work_dir / "vision-usage.jsonl")
        final_metrics = _usage_final_metrics(usage)
        if children:
            for target, field in (
                ("total_input_tokens", "input_tokens"),
                ("total_output_tokens", "output_tokens"),
                ("total_cache_read_tokens", "cache_read_tokens"),
                ("total_cache_creation_tokens", "cache_creation_tokens"),
            ):
                parent_total = sum(getattr(step.metrics, field) or 0 for step in builder.trajectory.steps if step.metrics)
                child_total = sum(getattr(child.final_metrics, target) for child in children if child.final_metrics)
                final_metrics[target] = max(final_metrics.get(target, 0), parent_total) + child_total
        if image_usage:
            builder.trajectory.extra.setdefault("openclaw_cli", {})[
                "image_model_usage"
            ] = image_usage
            builder.add_step(
                source="system", message="OpenClaw image model usage",
                metrics=StepMetrics(
                    input_tokens=image_usage["input_write_tokens"],
                    output_tokens=image_usage["output_tokens"],
                    cache_read_tokens=image_usage["cache_read_tokens"],
                    cache_creation_tokens=image_usage["cache_creation_tokens"],
                ),
            )
            for target, source in (
                ("total_input_tokens", "input_write_tokens"),
                ("total_output_tokens", "output_tokens"),
                ("total_cache_read_tokens", "cache_read_tokens"),
                ("total_cache_creation_tokens", "cache_creation_tokens"),
            ):
                if target in final_metrics:
                    final_metrics[target] += image_usage[source]
            builder.mark_cost_unavailable()
        if final_metrics:
            builder.override_final_metrics(**final_metrics)

        if not transcript_file.exists():
            builder.add_step(
                source="system",
                message="openclaw-cli: no session transcript available",
                extra={"reason": "no_transcript"},
            )

        builder.trajectory.extra.setdefault("openclaw_cli", {}).update({
            "exit_code": run_result.exit_code,
        })

    @classmethod
    def _consume_session_event(cls, event: dict, builder: TrajectoryBuilder) -> None:
        first_new_step = len(builder.trajectory.steps)
        etype = event.get("type")
        if etype == "message":
            cls._consume_message(event, builder)
        elif etype == "tool_result":
            cls._consume_tool_result(event, builder)
        # OpenClaw transcripts are imported after the run. The builder's default
        # timestamp is the import time, not when the action actually happened.
        if timestamp := _session_event_timestamp(event):
            for step in builder.trajectory.steps[first_new_step:]:
                step.timestamp = timestamp

    @staticmethod
    def _consume_message(event: dict, builder: TrajectoryBuilder) -> None:
        message = event.get("message", {})
        role = message.get("role", "")
        content_blocks = message.get("content", [])
        if not isinstance(content_blocks, list):
            content_blocks = []

        usage = message.get("usage", {})
        # openclaw reports per-message cost under usage.cost.total (it prices the
        # call itself); extract it so the trajectory's total_cost_usd is real.
        _cost = (usage.get("cost") or {}).get("total") if isinstance(usage, dict) else None
        metrics = StepMetrics(
            input_tokens=usage.get("input"),
            output_tokens=usage.get("output"),
            cache_read_tokens=usage.get("cacheRead"),
            cache_creation_tokens=usage.get("cacheWrite"),
            cost_usd=_cost,
        ) if usage else None

        if role == "assistant":
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            for block in content_blocks:
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif btype in ("toolCall", "tool_use"):
                    name = block.get("name", "")
                    args = block.get("arguments") or block.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"raw": args}
                    tool_calls.append(ToolCall(
                        id=block.get("id", ""),
                        name=name,
                        arguments=args,
                    ))

            builder.add_step(
                source="agent",
                message="\n".join(p for p in text_parts if p) or None,
                reasoning="\n".join(p for p in reasoning_parts if p) or None,
                tool_calls=tool_calls,
                metrics=metrics,
            )
        elif role == "user":
            text_parts = []
            results: list[ToolResult] = []
            for block in content_blocks:
                btype = block.get("type", "")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_result":
                    content = block.get("content")
                    parts: list[ContentPart] = []
                    if isinstance(content, str):
                        parts.append(ContentPart(type="text", text=content))
                    elif isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "text":
                                parts.append(ContentPart(type="text", text=c.get("text", "")))
                            elif c.get("type") == "image" and c.get("data"):
                                # openclaw/CUA returns MCP-style image blocks:
                                # {"type":"image","data":"<base64>","mimeType":...}.
                                # Keep them so persist_screenshots() can extract
                                # them to screenshots/ and rewrite to path refs.
                                parts.append(ContentPart(
                                    type="image",
                                    image=ImageSource(
                                        type="base64",
                                        media_type=c.get("mimeType", "image/png"),
                                        data=c.get("data"),
                                    ),
                                ))
                            elif c.get("type") == "image" and c.get("path"):
                                parts.append(ContentPart(
                                    type="image",
                                    image=ImageSource(
                                        type="path",
                                        media_type=c.get(
                                            "mimeType",
                                            "image/png",
                                        ),
                                        path=c.get("path"),
                                    ),
                                ))
                    results.append(ToolResult(
                        tool_call_id=block.get("tool_use_id") or block.get("call_id", ""),
                        content=parts,
                        is_error=bool(block.get("is_error")),
                    ))
            if results:
                builder.add_step(
                    source="environment",
                    observation=Observation(results=results),
                )
            elif text_parts:
                builder.add_step(
                    source="user",
                    message="\n".join(p for p in text_parts if p),
                )
        elif role == "toolResult":
            # openclaw emits tool results as their own message (role
            # "toolResult") with flat content blocks — text plus, for cua
            # screenshots, an {type:"image", data, mimeType} block. The previous
            # code read a non-existent top-level `output` field, which dropped
            # both the text AND the image, so persist_screenshots() never saw
            # the screenshot and screenshots/ stayed empty. Parse the content
            # blocks and keep image blocks as image ContentParts.
            parts: list[ContentPart] = []
            for block in content_blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(ContentPart(type="text", text=block.get("text", "")))
                elif btype == "image" and block.get("data"):
                    parts.append(ContentPart(
                        type="image",
                        image=ImageSource(
                            type="base64",
                            media_type=block.get("mimeType", "image/png"),
                            data=block.get("data"),
                        ),
                    ))
                elif btype == "image" and block.get("path"):
                    parts.append(ContentPart(
                        type="image",
                        image=ImageSource(
                            type="path",
                            media_type=block.get("mimeType", "image/png"),
                            path=block.get("path"),
                        ),
                    ))
            if not parts:
                # Back-compat: a flat string output on the event/message.
                out = event.get("output") or message.get("output", "")
                parts = [ContentPart(type="text", text=str(out))]
            call_id = (
                message.get("toolCallId")
                or event.get("tool_use_id")
                or event.get("call_id", "")
            )
            builder.add_step(
                source="environment",
                observation=Observation(results=[
                    ToolResult(
                        tool_call_id=call_id,
                        content=parts,
                        is_error=bool(message.get("isError") or event.get("is_error")),
                    ),
                ]),
            )

    @staticmethod
    def _consume_tool_result(event: dict, builder: TrajectoryBuilder) -> None:
        output = event.get("output", "")
        call_id = event.get("tool_use_id") or event.get("call_id", "")
        builder.add_step(
            source="environment",
            observation=Observation(results=[
                ToolResult(
                    tool_call_id=call_id,
                    content=[ContentPart(type="text", text=str(output))],
                    is_error=bool(event.get("is_error")),
                ),
            ]),
        )


def _session_event_timestamp(event: dict) -> str | None:
    """Return an event's actual UTC timestamp when the transcript provides one."""
    message = event.get("message")
    candidates = (event.get("timestamp"), message.get("timestamp") if isinstance(message, dict) else None)
    for value in candidates:
        try:
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    continue
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
                parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
            else:
                continue
            return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            continue
    return None


def _parse_stderr_json(stderr: str) -> dict | None:
    """Find JSON envelope in openclaw --json stderr stream."""
    if not stderr:
        return None

    lines = stderr.splitlines()
    # Strategy 1: line-based — first line that is just "{"
    for i, line in enumerate(lines):
        if line.strip() == "{":
            payload = "\n".join(lines[i:])
            try:
                value, _ = json.JSONDecoder().raw_decode(payload.lstrip())
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                break

    # Strategy 2: backward brace-balance from last "}"
    text = stderr.rstrip()
    if text.endswith("}"):
        end = len(text) - 1
        depth = 1
        in_str = False
        for j in range(end - 1, -1, -1):
            ch = text[j]
            if in_str:
                if ch == '"' and (j == 0 or text[j - 1] != "\\"):
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[j:end + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def _result_from_logs(*paths: Path) -> dict | None:
    decoder = json.JSONDecoder()
    for path in paths:
        text = _read_text_tolerant(path)
        index = 0
        while (index := text.find("{", index)) >= 0:
            try:
                candidate, length = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                index += 1
                continue
            if isinstance(candidate, dict) and candidate.get("status") == "ok":
                candidate = candidate.get("result")
            if _is_result_envelope(candidate):
                return candidate
            index += length
    return None


def _usage_final_metrics(usage: object) -> dict[str, int]:
    """Map an authoritative OpenClaw aggregate usage object to ALE totals."""
    if not isinstance(usage, dict):
        return {}
    field_map = {
        "input": "total_input_tokens",
        "output": "total_output_tokens",
        "cacheRead": "total_cache_read_tokens",
        "cacheWrite": "total_cache_creation_tokens",
    }
    totals = {
        target: int(value)
        for source, target in field_map.items()
        if isinstance((value := usage.get(source)), (int, float))
        and not isinstance(value, bool)
        and value >= 0
    }
    return totals if any(totals.values()) else {}


_DIAG_SIGNAL_PATTERNS = (
    "FailoverError",
    "No API key found",
    "lane task error",
    "model fallback decision",
    "Error:",
)


def _diagnose_failure(stderr_log: Path, exit_code: int | None) -> str:
    parts = [f"agent failed (rc={exit_code})"]
    text = _read_text_tolerant(stderr_log)
    if text.strip():
        # The model-catalog ESM warning (ERR_MODULE_NOT_FOUND) is emitted
        # transiently while bundled plugins stage their runtime deps and is
        # caught/non-fatal — it tends to occupy the literal tail and mask the
        # real cause. Surface the highest-signal diagnostic lines first.
        lines = [ln for ln in text.splitlines() if ln.strip()]
        signal = [
            ln for ln in lines
            if any(pat in ln for pat in _DIAG_SIGNAL_PATTERNS)
            and "ERR_MODULE_NOT_FOUND" not in ln
        ]
        if signal:
            parts.append("stderr signals: " + " || ".join(signal[-5:]))
        parts.append(f"stderr tail: ...{text[-1200:]}")
    return " | ".join(parts)


def _is_result_envelope(candidate: dict | None) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("payloads"), list)
        and isinstance(candidate.get("meta"), dict)
        and isinstance(candidate["meta"].get("durationMs"), (int, float))
    )


def _result_envelope_error(result_envelope: dict | None) -> str | None:
    if result_envelope is None:
        return None
    meta = result_envelope.get("meta", {})
    if meta.get("aborted") is True:
        payload_text = next(
            (
                payload["text"]
                for payload in result_envelope.get("payloads", [])
                if isinstance(payload, dict)
                and isinstance(payload.get("text"), str)
                and payload["text"].strip()
            ),
            "OpenClaw reported an aborted agent run",
        )
        return f"agent aborted: {payload_text}"
    if (
        meta.get("stopReason") == "stop"
        and meta.get("replayInvalid") is True
        and not any(
            isinstance(meta.get(field), str) and meta[field].strip()
            for field in ("finalAssistantVisibleText", "finalAssistantRawText")
        )
    ):
        return (
            "OpenClaw completed with no usable terminal assistant response; "
            "the episode must be retried"
        )
    return None


def _read_text_tolerant(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""
