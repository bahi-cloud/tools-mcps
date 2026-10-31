# hub.py
# مدير الأدوات الموحد (MCP + Local) مع Circuit Breaker محسّن ومعالجة متقدمة
# ★ إصدار جراحي: دعم ${ENV_VAR} في headers و env + عزل مستخدمين فعلي
# ★ FIX (هذه النسخة): الإقلاع لا يمكن أن يعلق بسبب سيرفر MCP واحد
#     1) مهلة startup_timeout لكل سيرفر (افتراضي 60 ثانية) عبر asyncio.timeout
#     2) تنظيف الاتصال عند الإلغاء/المهلة (BaseException) حتى لا تبقى processes يتيمة
#     3) إغلاق جلسة السيرفر الفاشل بدل تركها مفتوحة
#     (لا تغيير في منطق الأدوات أو Circuit Breaker أو عزل المستخدمين)

import json
import logging
import asyncio
import inspect
import time
import os
import re
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from contextlib import AsyncExitStack
from fastapi import Request

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from tools import TOOLS as COMPOSIO_TOOLS, LocalTool
from dependency_injector import DependencyInjector

logger = logging.getLogger("hub")

SPACE_BASE_URL = os.environ.get("SPACE_BASE_URL", "")


def _safe_client_name(client_id: str) -> str:
    """اسم مجلد آمن وثابت لكل هوية (ASCII فقط). يجب أن تطابق نسخة hub.py تماماً."""
    cid = (client_id or "").strip()
    if not cid:
        return "anonymous"
    clean = "".join(c for c in cid if c.isascii() and (c.isalnum() or c in "-_"))
    if clean != cid or len(clean) > 64:
        return "c_" + hashlib.sha256(cid.encode("utf-8")).hexdigest()[:32]
    return clean


def _full_url(relative_path: str) -> str:
    base = SPACE_BASE_URL.rstrip("/") if SPACE_BASE_URL else ""
    return f"{base}{relative_path}" if base else relative_path

# ★ المهلة الافتراضية لإقلاع كل سيرفر MCP (اتصال + list_tools)
DEFAULT_STARTUP_TIMEOUT = float(os.environ.get("MCP_STARTUP_TIMEOUT", "60"))


# ══════════════════════════════════════════════════════════
# ★ حل متغيرات البيئة ${VAR} (قراءة كسولة عند الاتصال)
# ══════════════════════════════════════════════════════════

def _resolve_env_vars(value):
    """يستبدل ${VAR_NAME} بقيمة متغير البيئة. يدعم str/dict/list متداخلة."""
    if isinstance(value, str):
        return re.sub(
            r'\$\{(\w+)\}',
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value
        )
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


# ══════════════════════════════════════════════════════════
# استثناءات
# ══════════════════════════════════════════════════════════

class ToolError(Exception): pass
class ToolNotFound(ToolError): pass
class ToolDisabled(ToolError): pass
class ToolExecutionError(ToolError): pass
class CircuitOpenError(ToolError): pass


# ══════════════════════════════════════════════════════════
# AccessContext
# ══════════════════════════════════════════════════════════

class AccessContext:
    def __init__(self, client_id="internal", allowed_servers=None, allowed_domains=None, allowed_tags=None, allowed_tools=None):
        self.client_id = client_id
        self.allowed_servers = allowed_servers
        self.allowed_domains = allowed_domains
        self.allowed_tags = allowed_tags
        self.allowed_tools = allowed_tools


# ══════════════════════════════════════════════════════════
# ToolRoute
# ══════════════════════════════════════════════════════════

@dataclass
class ToolRoute:
    public_name: str
    server_name: str
    remote_name: str
    runtime_generation: int
    description: str
    input_schema: Dict[str, Any]
    domain: str
    tags: Tuple[str, ...]


# ══════════════════════════════════════════════════════════
# CircuitBreaker
# ══════════════════════════════════════════════════════════

@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    reset_timeout: int = 60
    _failure_count: int = 0
    _last_failure_time: float = 0
    _state: str = "CLOSED"
    _total_requests: int = 0
    _successful_requests: int = 0

    async def call_async(self, coro, *args, timeout: float = 30.0):
        self._total_requests += 1
        if self._state == "OPEN":
            if time.time() - self._last_failure_time > self.reset_timeout:
                self._state = "HALF_OPEN"
                logger.info(f"🔓 Circuit Breaker '{self.name}' -> HALF_OPEN")
            else:
                logger.warning(f"⛔ Circuit Breaker '{self.name}' is OPEN")
                raise CircuitOpenError(f"Service '{self.name}' temporarily unavailable")
        try:
            result = await asyncio.wait_for(coro(*args), timeout=timeout)
            self._successful_requests += 1
            if self._state == "HALF_OPEN":
                self._state = "CLOSED"
                self._failure_count = 0
                logger.info(f"✅ Circuit Breaker '{self.name}' -> CLOSED")
            return result
        except asyncio.TimeoutError as e:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = "OPEN"
                logger.error(f"🚨 Circuit Breaker '{self.name}' -> OPEN")
            raise ToolExecutionError(f"Tool timed out after {timeout}s") from e
        except Exception as e:
            self._failure_count += 1
            self._last_failure_time = time.time()
            if self._failure_count >= self.failure_threshold:
                self._state = "OPEN"
                logger.error(f"🚨 Circuit Breaker '{self.name}' -> OPEN")
            raise e

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = "OPEN"

    def reset(self):
        self._state = "CLOSED"
        self._failure_count = 0
        self._last_failure_time = 0
        logger.info(f"🔄 Circuit Breaker '{self.name}' manually reset")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state,
            "failure_count": self._failure_count,
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "success_rate": f"{self._successful_requests / max(1, self._total_requests) * 100:.1f}%"
        }


# ══════════════════════════════════════════════════════════
# MCPSession
# ══════════════════════════════════════════════════════════

class MCPSession:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.session: Optional[ClientSession] = None
        self.exit_stack: Optional[AsyncExitStack] = None

    async def connect(self) -> ClientSession:
        transport = self.config.get("transport", "stdio")
        self.exit_stack = AsyncExitStack()

        try:
            if transport == "stdio":
                command = self.config.get("command")
                args = self.config.get("args", [])
                if not command:
                    raise ValueError("Command required for stdio transport")

                # ★ حل ${ENV_VAR} في env
                env_overrides = _resolve_env_vars(self.config.get("env", {}))
                if env_overrides:
                    env = {**os.environ, **env_overrides}
                else:
                    env = None

                params = StdioServerParameters(command=command, args=args, env=env)
                read, write = await self.exit_stack.enter_async_context(stdio_client(params))
                self.session = await self.exit_stack.enter_async_context(ClientSession(read, write))

            elif transport == "streamable-http":
                url = self.config.get("url")
                if not url:
                    raise ValueError("URL required for streamable-http transport")

                # ★ حل ${ENV_VAR} في headers
                headers = _resolve_env_vars(self.config.get("headers", {}))
                read, write, _ = await self.exit_stack.enter_async_context(
                    streamablehttp_client(url, headers=headers)
                )
                self.session = await self.exit_stack.enter_async_context(ClientSession(read, write))

            else:
                raise ValueError(f"Unsupported transport: {transport}")

            await self.session.initialize()
            logger.info(f"✅ Connected to MCP server: {transport}")
            return self.session

        except BaseException:
            # ★ FIX: BaseException (وليس Exception فقط) ليشمل الإلغاء/المهلة
            #   فلا تبقى processes أو اتصالات يتيمة
            if self.exit_stack:
                try:
                    await self.exit_stack.aclose()
                except BaseException as close_err:
                    logger.warning(f"Error cleaning up failed connection: {close_err!r}")
                self.exit_stack = None
            self.session = None
            raise

    async def list_tools(self) -> List[Any]:
        if not self.session:
            await self.connect()
        result = await self.session.list_tools()
        return result.tools

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        if not self.session:
            await self.connect()
        return await self.session.call_tool(tool_name, arguments)

    async def close(self) -> None:
        if self.exit_stack:
            try:
                await self.exit_stack.aclose()
            except BaseException as e:
                logger.warning(f"Error closing session: {e!r}")
            finally:
                self.session = None
                self.exit_stack = None


# ══════════════════════════════════════════════════════════
# LocalRuntime
# ══════════════════════════════════════════════════════════

class LocalRuntime:
    def __init__(self, tool: LocalTool, injector: DependencyInjector):
        self.tool = tool
        self.injector = injector

    async def invoke(
        self,
        user_arguments: Dict[str, Any],
        request: Optional[Request] = None,
        client_id: str = "anonymous",
    ) -> Any:
        try:
            full_args = self.injector.resolve_dependencies(
                tool_name=self.tool.name,
                user_args=user_arguments
            )
            sig = inspect.signature(self.tool.callable)
            if request is not None and "request" in sig.parameters:
                full_args["request"] = request

            # ★ FIX: عزل الملفات لكل عميل. نفرض files_dir = files/<client_id>/ دائماً
            #   (حتى لو وفّره الـ injector) بدل المجلد المشترك files/
            safe_client = _safe_client_name(client_id)
            user_dir = None
            if "files_dir" in sig.parameters:
                user_dir = Path.cwd() / "files" / safe_client
                user_dir.mkdir(parents=True, exist_ok=True)
                full_args["files_dir"] = user_dir

            if asyncio.iscoroutinefunction(self.tool.callable):
                result = await self.tool.callable(**full_args)
            else:
                result = self.tool.callable(**full_args)

            # ★ FIX: تصحيح الرابط ليشمل مجلد العميل ويصبح كاملاً
            if user_dir is not None and isinstance(result, dict) and result.get("local_path"):
                try:
                    p = Path(result["local_path"])
                    if p.parent == user_dir:
                        rel = f"/files/{safe_client}/{p.name}"
                        result["relative_path"] = rel
                        result["file_url"] = _full_url(rel)
                except Exception:
                    pass
            return result
        except Exception as e:
            raise ToolExecutionError(f"Local tool error: {e}")


# ══════════════════════════════════════════════════════════
# MCPRegistry
# ══════════════════════════════════════════════════════════

class MCPRegistry:
    def __init__(self, config_file: str = "mcps.json"):
        self.config_file = config_file
        self.local_runtimes: Dict[str, LocalRuntime] = {}
        self.local_aliases: Dict[str, str] = {}
        self.tool_routes: Dict[str, ToolRoute] = {}
        self.injector: Optional[DependencyInjector] = None

        self.mcp_sessions: Dict[str, MCPSession] = {}
        self.mcp_tools_map: Dict[str, str] = {}
        self.servers: Dict[str, Any] = {}

        self.circuit_breakers: Dict[str, CircuitBreaker] = {}

        self._load_local_tools()

    def _get_circuit_breaker(self, server_name: str) -> CircuitBreaker:
        if server_name not in self.circuit_breakers:
            self.circuit_breakers[server_name] = CircuitBreaker(
                name=server_name,
                failure_threshold=5,
                reset_timeout=30
            )
        return self.circuit_breakers[server_name]

    def set_injector(self, injector: DependencyInjector) -> None:
        self.injector = injector
        self._load_local_tools()

    def _load_local_tools(self):
        if self.injector is None:
            from dependency_injector import create_default_injector
            self.injector = create_default_injector()

        from tools import TOOLS as COMPOSIO_TOOLS, LocalTool

        composio_count = 0
        for name, tool in COMPOSIO_TOOLS.items():
            if not tool.enabled:
                continue
            if name in self.local_runtimes:
                continue

            runtime = LocalRuntime(tool, self.injector)
            self.local_runtimes[name] = runtime

            for alias in tool.aliases:
                self.local_aliases[alias] = name

            self.tool_routes[name] = ToolRoute(
                public_name=name,
                server_name="local",
                remote_name=name,
                runtime_generation=0,
                description=tool.description,
                input_schema=tool.parameters,
                domain=tool.category.lower(),
                tags=tuple(tool.tags) + ("local",)
            )
            composio_count += 1

        logger.info(f"✅ Added {composio_count} Composio tools to existing {len(self.local_runtimes) - composio_count} local tools")
        logger.info(f"✅ Total local tools: {len(self.local_runtimes)}")

    async def startup_from_config(self) -> Dict[str, Any]:
        config_path = Path(self.config_file)
        if not config_path.exists():
            logger.warning(f"⚠️ {self.config_file} not found")
            return {"started": [], "errors": {self.config_file: "File not found"}}

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # ★ حل ${ENV_VAR} على مستوى التكوين كامل
        config = _resolve_env_vars(config)

        started = []
        errors = {}

        for name, server_config in config.get("servers", {}).items():
            if not server_config.get("enabled", True):
                continue

            # ★ FIX: مهلة لكل سيرفر (قابلة للتعديل per-server بالمفتاح startup_timeout في mcps.json)
            try:
                startup_timeout = float(server_config.get("startup_timeout", DEFAULT_STARTUP_TIMEOUT))
            except (TypeError, ValueError):
                startup_timeout = DEFAULT_STARTUP_TIMEOUT

            session: Optional[MCPSession] = None
            try:
                session = MCPSession(server_config)
                # asyncio.timeout (Python 3.11+) يعمل في نفس الـ task، فلا يكسر cancel scopes الخاصة بـ anyio
                async with asyncio.timeout(startup_timeout):
                    await session.connect()
                    tools = await session.list_tools()

                self.mcp_sessions[name] = session
                prefix = server_config.get("tool_prefix", name)

                for tool in tools:
                    tool_name = tool.name
                    public_name = f"{prefix}__{tool_name}" if prefix else tool_name

                    self.tool_routes[public_name] = ToolRoute(
                        public_name=public_name,
                        server_name=name,
                        remote_name=tool_name,
                        runtime_generation=1,
                        description=tool.description or "",
                        input_schema=tool.inputSchema or {},
                        domain=server_config.get("domain", "general"),
                        tags=tuple(server_config.get("tags", []))
                    )
                    self.mcp_tools_map[public_name] = name

                self.servers[name] = server_config
                started.append(name)
                logger.info(f"✅ Started server '{name}' with {len(tools)} tools")

            except TimeoutError:
                errors[name] = f"startup timeout after {startup_timeout}s"
                logger.error(f"⏱️ Server '{name}' did not start within {startup_timeout}s — skipped")
                if session is not None:
                    await session.close()
            except Exception as e:
                errors[name] = str(e)
                logger.error(f"❌ Failed to start server '{name}': {e}")
                if session is not None:
                    await session.close()

        return {"started": started, "errors": errors}

    async def shutdown(self) -> None:
        for name, session in list(self.mcp_sessions.items()):
            try:
                await session.close()
                logger.info(f"✅ Closed session for '{name}'")
            except Exception as e:
                logger.error(f"❌ Failed to close session for '{name}': {e}")
        self.mcp_sessions.clear()
        self.mcp_tools_map.clear()
        self.servers.clear()

    async def call_tool(self, public_name: str, arguments: Dict[str, Any], context: Optional[AccessContext] = None, request: Optional[Request] = None) -> Dict[str, Any]:

        client_id = getattr(context, "client_id", "internal")
        logger.info(f"🔑 call_tool: client_id={client_id}, tool={public_name}")

        if context is not None and context.allowed_tools is not None:
            if public_name not in context.allowed_tools:
                raise ToolNotFound(f"Tool '{public_name}' not allowed for client '{client_id}'")

        if context is not None and context.allowed_servers is not None:
            route = self.tool_routes.get(public_name)
            if route is not None and route.server_name not in context.allowed_servers:
                raise ToolNotFound(f"Server '{route.server_name}' not allowed for client '{client_id}'")

        real_name = self.local_aliases.get(public_name, public_name)
        if real_name in self.local_runtimes:
            try:
                result = await self.local_runtimes[real_name].invoke(arguments, request=request, client_id=client_id)
                return {"ok": True, "result": result}
            except ToolExecutionError:
                raise
            except Exception as e:
                raise ToolExecutionError(f"Local tool error: {e}")

        if public_name in self.mcp_tools_map:
            server_name = self.mcp_tools_map[public_name]
            session = self.mcp_sessions.get(server_name)
            if not session:
                raise ToolExecutionError(f"MCP server '{server_name}' not connected")

            tool_route = self.tool_routes.get(public_name)
            if not tool_route:
                raise ToolNotFound(f"Tool '{public_name}' route not found")

            server_config = self.servers.get(server_name, {})
            timeout = server_config.get("timeout", 30.0)

            cb = self._get_circuit_breaker(server_name)
            start_time = time.time()

            logger.info(f"🔧 Calling MCP tool: {public_name} (timeout: {timeout}s)")

            try:
                result = await cb.call_async(
                    session.call_tool,
                    tool_route.remote_name,
                    arguments,
                    timeout=timeout
                )
                elapsed = time.time() - start_time
                logger.info(f"✅ MCP tool '{public_name}' completed in {elapsed:.2f}s")
                content = self._process_response(result)
                return {"ok": True, "result": content}
            except CircuitOpenError as e:
                raise ToolExecutionError(f"Service '{server_name}' temporarily unavailable (circuit open)")
            except Exception as e:
                logger.error(f"❌ MCP tool '{public_name}' failed: {e}")
                if not isinstance(e, ToolExecutionError):
                    cb.record_failure()
                raise ToolExecutionError(f"MCP tool error: {e}")

        raise ToolNotFound(f"Tool '{public_name}' not found")

    def _process_response(self, result) -> str:
        parts = []
        for item in result.content:
            if hasattr(item, 'text') and item.text:
                parts.append(item.text)
            elif hasattr(item, 'data'):
                mime = getattr(item, 'mimeType', 'image')
                parts.append(f"[{mime}: {len(item.data)} bytes]")
            elif hasattr(item, 'resource'):
                resource = item.resource
                uri = getattr(resource, 'uri', 'unknown')
                mime = getattr(resource, 'mimeType', 'unknown')
                parts.append(f"[Resource: {uri} ({mime})]")
            else:
                parts.append(str(item))
        return "\n".join(parts) if parts else "No content returned"

    def list_tools(self) -> Dict[str, Any]:
        mcp_tools = []
        for name, route in self.tool_routes.items():
            if route.server_name != "local":
                mcp_tools.append({
                    "name": name,
                    "description": route.description,
                    "parameters": route.input_schema,
                    "category": route.domain or "General",
                    "type": "mcp"
                })

        local_tools = []
        for name, tool in COMPOSIO_TOOLS.items():
            if not tool.hidden and tool.enabled:
                local_tools.append({
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "category": tool.category,
                    "type": "local"
                })

        all_tools = mcp_tools + local_tools

        categories = {}
        for tool in all_tools:
            cat = tool.get("category", "General")
            if cat not in categories:
                categories[cat] = {"name": cat, "icon": "📦", "tools": []}
            categories[cat]["tools"].append(tool["name"])

        return {"tools": all_tools, "categories": categories}

    def get_circuit_breaker_status(self) -> Dict[str, Any]:
        return {name: cb.get_stats() for name, cb in self.circuit_breakers.items()}

    def reset_circuit_breaker(self, server_name: str) -> bool:
        if server_name in self.circuit_breakers:
            self.circuit_breakers[server_name].reset()
            return True
        return False


# ══════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════

class MockAuth:
    async def reload(self): pass
    async def authenticate(self, api_key): return None

def create_services(config_file: str = "mcps.json") -> tuple:
    registry = MCPRegistry(config_file)
    auth = MockAuth()
    return registry, auth