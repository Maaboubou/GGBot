"""
插件管理器 - 负责插件的加载、卸载、热重载
"""

import json
import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import time
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from .event_bus import EventBus, EventType, Event, get_event_bus
from .plugin_manifest import (
    PluginManifestError,
    listener_manifest_map,
    load_plugin_manifest,
    validate_registered_listeners,
)
from .routing_order import RoutingOrderStore


@dataclass
class PluginInfo:
    """插件信息"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    path: str = ""
    enabled: bool = True
    loaded: bool = False
    module: Optional[Any] = None
    config: Dict[str, Any] = None
    listener_ids: List[str] = None
    manifest: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.config is None:
            self.config = {}
        if self.listener_ids is None:
            self.listener_ids = []
        if self.manifest is None:
            self.manifest = {}


def purge_plugin_package_modules(package_name: str) -> List[str]:
    """清除插件包与所有子模块，返回被清除的模块名。"""

    package_module = sys.modules.get(package_name)
    cached_module_names = [
        module_name
        for module_name in list(sys.modules)
        if module_name == package_name or module_name.startswith(package_name + ".")
    ]
    for module_name in sorted(cached_module_names, reverse=True):
        sys.modules.pop(module_name, None)

    parent_name, _, child_name = package_name.rpartition(".")
    parent_module = sys.modules.get(parent_name)
    if (
        package_module is not None
        and parent_module is not None
        and getattr(parent_module, child_name, None) is package_module
    ):
        delattr(parent_module, child_name)

    return cached_module_names


class PluginFileHandler(FileSystemEventHandler):
    """插件文件变化监听器"""
    
    def __init__(self, plugin_manager: 'PluginManager'):
        self.plugin_manager = plugin_manager
        self.logger = logging.getLogger(__name__)
        self._last_reload_time = {}
        self._reload_delay = 1.0  # 1秒防抖
    
    def on_modified(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        if file_path.suffix != '.py':
            return
        
        # 防抖处理
        now = time.time()
        if file_path in self._last_reload_time:
            if now - self._last_reload_time[file_path] < self._reload_delay:
                return
        self._last_reload_time[file_path] = now
        
        # 确定插件名
        plugin_name = None
        for name, plugin_info in self.plugin_manager.plugins.items():
            if file_path.parent == Path(plugin_info.path):
                plugin_name = name
                break
        
        if plugin_name and self.plugin_manager.plugins[plugin_name].enabled:
            self.logger.info(f"Detected change in plugin '{plugin_name}', reloading...")
            threading.Thread(
                target=self.plugin_manager._reload_plugin_thread,
                args=(plugin_name,),
                daemon=True
            ).start()
    
    def on_created(self, event):
        if event.is_directory:
            # 新增插件目录
            plugin_path = Path(event.src_path)
            config_file = plugin_path / "config.json"
            if config_file.exists():
                self.logger.info(f"Detected new plugin directory: {plugin_path.name}")
                threading.Thread(
                    target=self.plugin_manager._discover_and_load_new_plugin,
                    args=(str(plugin_path),),
                    daemon=True
                ).start()
    
    def on_deleted(self, event):
        if event.is_directory:
            # 插件目录被删除
            plugin_path = Path(event.src_path)
            plugin_name = plugin_path.name
            if plugin_name in self.plugin_manager.plugins:
                self.logger.info(f"Plugin directory deleted: {plugin_name}")
                threading.Thread(
                    target=self.plugin_manager.unload_plugin,
                    args=(plugin_name,),
                    daemon=True
                ).start()


class PluginManager:
    """插件管理器核心类"""
    
    def __init__(self, plugins_dir: str = "app/plugins", event_bus: Optional[EventBus] = None):
        self.plugins_dir = Path(plugins_dir)
        self.event_bus = event_bus or get_event_bus()
        self.logger = logging.getLogger(__name__)
        
        self.plugins: Dict[str, PluginInfo] = {}
        self._lock = threading.RLock()
        self.routing_order = RoutingOrderStore(self.plugins_dir / "routing_order.json")
        
        # 文件监控
        self._observer = Observer()
        self._file_handler = PluginFileHandler(self)
        
        # 确保插件目录存在
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
    
    def start_monitoring(self):
        """启动文件监控"""
        try:
            self._observer.schedule(self._file_handler, str(self.plugins_dir), recursive=True)
            self._observer.start()
            self.logger.debug(f"Started monitoring plugin directory: {self.plugins_dir}")
        except Exception as e:
            self.logger.error(f"Failed to start file monitoring: {e}")
    
    def stop_monitoring(self):
        """停止文件监控"""
        try:
            self._observer.stop()
            self._observer.join()
            self.logger.info("Stopped plugin file monitoring")
        except Exception as e:
            self.logger.error(f"Error stopping file monitoring: {e}")
    
    def discover_plugins(self):
        """发现所有插件并预加载信息（支持递归子目录）"""
        self.logger.debug("Discovering plugins...")
        if not self.plugins_dir.exists():
            self.logger.warning(f"Plugins directory not found: {self.plugins_dir}")
            return

        # 递归查找包含 config.json 与 main.py 的目录
        for config_path in self.plugins_dir.rglob("config.json"):
            plugin_dir = config_path.parent
            if plugin_dir.name.startswith('_'):
                continue
            main_file = plugin_dir / "main.py"
            if not main_file.exists():
                continue

            # 以相对路径作为键，允许多级目录，如 "feishu/feishu_fetch_demo"
            rel_path = plugin_dir.relative_to(self.plugins_dir)
            plugin_key = str(rel_path).replace('\\', '/')

            if plugin_key in self.plugins:
                continue

            # 独立加载配置文件（直接从路径读取）
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            except Exception as e:
                self.logger.error(f"Failed to load config for plugin '{plugin_key}': {e}")
                continue

            plugin_info = PluginInfo(
                name=config.get('name', plugin_dir.name),
                version=config.get('version', '1.0.0'),
                description=config.get('description', ''),
                author=config.get('author', ''),
                path=str(plugin_dir),
                enabled=bool((config.get('runtime') or {}).get('enabled', True)),
                config=config
            )
            self.plugins[plugin_key] = plugin_info
            self.logger.debug(f"Found plugin: {plugin_key}")

        self.logger.debug(f"Discovery complete. Found {len(self.plugins)} total plugins.")

    def load_plugin_config(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """加载插件配置"""
        # First try to get plugin info to get the actual path
        plugin_info = self.plugins.get(plugin_name)
        
        if plugin_info and plugin_info.path:
            # Use the actual plugin path
            config_file = Path(plugin_info.path) / "config.json"
        else:
            # Fallback to using plugin_name directly
            config_file = self.plugins_dir / plugin_name / "config.json"
        
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except Exception as e:
            self.logger.error(f"Failed to load config for plugin '{plugin_name}': {e}")
            return None
    
    def load_plugin(self, plugin_name: str) -> bool:
        """加载插件"""
        with self._lock:
            if plugin_name in self.plugins and self.plugins[plugin_name].loaded:
                self.logger.warning(f"Plugin '{plugin_name}' is already loaded")
                return True
            
            # 支持多级目录：优先使用已发现的插件信息中的绝对路径
            if plugin_name in self.plugins:
                plugin_path = Path(self.plugins[plugin_name].path)
            else:
                plugin_path = self.plugins_dir / plugin_name
            if not plugin_path.exists():
                self.logger.error(f"Plugin directory not found: {plugin_path}")
                return False
            
            # 加载配置
            config = self.load_plugin_config(plugin_name)
            if not config:
                return False
            
            try:
                # 动态导入插件模块
                manifest = load_plugin_manifest(plugin_path, config)
                manifest_by_listener = listener_manifest_map(manifest)
                # 计算模块名：plugins.<相对路径用点分隔>.main
                rel_path = plugin_path.relative_to(self.plugins_dir)
                rel_module_path = str(rel_path).replace('\\', '.').replace('/', '.')
                rel_module_path = str(rel_path).replace('\\', '.').replace('/', '.')
                # 使用完整的包路径 app.plugins... 以匹配API中的导入
                module_name = "app.plugins." + rel_module_path + ".main"
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    plugin_path / "main.py"
                )
                if spec is None or spec.loader is None:
                    self.logger.error(f"Failed to create module spec for plugin '{plugin_name}'")
                    return False
                
                module = importlib.util.module_from_spec(spec)
                import sys
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                
                # 检查插件是否有注册函数
                if not hasattr(module, 'register'):
                    self.logger.error(f"Plugin '{plugin_name}' missing 'register' function")
                    return False
                
                
                # 获取或创建插件信息
                plugin_info = self.plugins.get(plugin_name)
                if not plugin_info:
                    # 插件信息不存在,创建新的(这可能发生在重新加载时)
                    plugin_info = PluginInfo(
                        name=plugin_name,
                        version=config.get("version", "1.0.0"),
                        description=config.get("description", ""),
                        author=config.get("author", ""),
                        path=str(plugin_path),
                        enabled=bool((config.get("runtime") or {}).get("enabled", True)),
                        config=config
                    )
                    self.plugins[plugin_name] = plugin_info
                else:
                    # 插件信息已存在,更新配置和元数据
                    plugin_info.version = config.get("version", "1.0.0")
                    plugin_info.description = config.get("description", "")
                    plugin_info.author = config.get("author", "")
                    plugin_info.config = config  # 更新配置为最新加载的数据
                
                plugin_info.module = module
                plugin_info.loaded = True
                plugin_info.manifest = manifest

                
                # 注册插件
                listener_ids = []
                listener_key_counts: Dict[str, int] = {}
                registered_identities = []

                def subscribe_wrapper(
                    event_type,
                    handler,
                    listener_key: Optional[str] = None,
                ):
                    event_value = event_type.value if isinstance(event_type, EventType) else str(event_type)
                    handler_name = getattr(handler, "__name__", handler.__class__.__name__)
                    base_key = listener_key or f"{event_value}:{handler_name}"
                    duplicate_index = listener_key_counts.get(base_key, 0)
                    listener_key_counts[base_key] = duplicate_index + 1
                    local_key = base_key if duplicate_index == 0 else f"{base_key}#{duplicate_index + 1}"
                    stable_key = (
                        local_key
                        if local_key.startswith(f"{plugin_name}:")
                        else f"{plugin_name}:{local_key}"
                    )

                    identity = (event_value, handler_name)
                    listener_spec = manifest_by_listener.get(identity)
                    if listener_spec is None:
                        raise PluginManifestError(
                            f"监听器 {event_value}:{handler_name} 未在 manifest.json 声明"
                        )
                    registered_identities.append(identity)
                    propagation = listener_spec["propagation"]
                    order_index = self.routing_order.index_for(event_value, stable_key)

                    listener_id = self.event_bus.subscribe(
                        event_type=event_type,
                        handler=handler,
                        plugin_name=plugin_name,
                        order_index=order_index,
                        propagation=propagation,
                        listener_key=stable_key,
                        handler_name=handler_name,
                        order_source="routing_order",
                        trigger_spec=listener_spec,
                    )
                    listener_ids.append(listener_id)
                    return listener_id
                
                # 调用插件注册函数
                module.register(self.event_bus, subscribe_wrapper)
                validate_registered_listeners(manifest, registered_identities)
                plugin_info.listener_ids = listener_ids
                if not plugin_info.enabled:
                    for listener_id in listener_ids:
                        self.event_bus.disable_listener(listener_id)
                
                # self.plugins[plugin_name] = plugin_info # 不再需要，因为我们是更新
                
                # 发布插件加载事件
                self.event_bus.publish(Event(
                    type=EventType.PLUGIN_LOADED,
                    source="plugin_manager",
                    data={
                        "plugin_name": plugin_name,
                        "plugin_info": {
                            "version": plugin_info.version,
                            "description": plugin_info.description,
                            "author": plugin_info.author
                        }
                    }
                ))
                
                self.logger.debug(f"Successfully loaded plugin '{plugin_name}' v{plugin_info.version}")
                return True
                
            except Exception as e:
                for listener_id in locals().get("listener_ids", []):
                    self.event_bus.unsubscribe(listener_id)
                plugin_info = self.plugins.get(plugin_name)
                if plugin_info is not None:
                    plugin_info.listener_ids = []
                    plugin_info.loaded = False
                    plugin_info.module = None
                self.logger.error(f"Failed to load plugin '{plugin_name}': {e}")
                return False
    
    def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                self.logger.warning(f"Plugin '{plugin_name}' not found")
                return False
            
            plugin_info = self.plugins[plugin_name]
            
            try:
                # 取消所有事件订阅
                for listener_id in plugin_info.listener_ids:
                    self.event_bus.unsubscribe(listener_id)
                
                # 调用插件的卸载函数（如果存在）
                if plugin_info.module and hasattr(plugin_info.module, 'unregister'):
                    plugin_info.module.unregister()
                
                # 从sys.modules中移除整个插件包。只移除 main.py 会让
                # getmagnet.py 等子模块在热重载后继续使用旧代码。
                plugin_path = Path(plugin_info.path)
                rel_path = plugin_path.relative_to(self.plugins_dir)
                rel_module_path = str(rel_path).replace('\\', '.').replace('/', '.')
                package_name = "app.plugins." + rel_module_path
                purge_plugin_package_modules(package_name)
                
                # 发布插件卸载事件
                self.event_bus.publish(Event(
                    type=EventType.PLUGIN_UNLOADED,
                    source="plugin_manager",
                    data={"plugin_name": plugin_name}
                ))
                
                del self.plugins[plugin_name]
                
                self.logger.debug(f"Successfully unloaded plugin '{plugin_name}'")
                return True
                
            except Exception as e:
                self.logger.error(f"Failed to unload plugin '{plugin_name}': {e}")
                return False

    def unload_all_plugins(self) -> Dict[str, bool]:
        """按加载顺序的逆序卸载全部插件，确保驱动等插件资源被释放。"""
        results: Dict[str, bool] = {}

        for plugin_name in reversed(list(self.plugins.keys())):
            results[plugin_name] = self.unload_plugin(plugin_name)

        unloaded_count = sum(results.values())
        self.logger.info("Unloaded %s/%s plugins", unloaded_count, len(results))
        return results
    
    def reload_plugin(self, plugin_name: str) -> bool:
        """重新加载插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return self.load_plugin(plugin_name)
            
            self.logger.debug(f"Reloading plugin '{plugin_name}'")
            
            # 先卸载
            if not self.unload_plugin(plugin_name):
                return False
            
            # 再加载
            return self.load_plugin(plugin_name)
    
    def _reload_plugin_thread(self, plugin_name: str):
        """在线程中重载插件"""
        time.sleep(0.5)  # 稍等一下让文件写入完成
        self.reload_plugin(plugin_name)
    
    def _discover_and_load_new_plugin(self, plugin_path: str):
        """发现并加载新插件"""
        time.sleep(1.0)  # 等待目录创建完成
        plugin_name = Path(plugin_path).name
        self.logger.debug(f"Loading new plugin: {plugin_name}")
        self.load_plugin(plugin_name)
    
    def load_all_plugins(self) -> Dict[str, bool]:
        """加载所有插件"""
        self.discover_plugins() # 先发现所有插件
        results = {}
        
        for plugin_name in list(self.plugins.keys()):
            if self.plugins[plugin_name].enabled:
                results[plugin_name] = self.load_plugin(plugin_name)
            else:
                # Disabled plugins remain discoverable/configurable without
                # importing their module or starting background work.
                results[plugin_name] = True
        
        loaded_count = sum(results.values())
        self.logger.info(f"Loaded {loaded_count}/{len(self.plugins)} plugins")
        
        return results
    
    def enable_plugin(self, plugin_name: str) -> bool:
        """启用插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return False

            plugin_info = self.plugins[plugin_name]
            if not self._save_plugin_runtime_enabled(plugin_name, True):
                return False
            plugin_info.enabled = True

            if not plugin_info.loaded:
                if not self.load_plugin(plugin_name):
                    self._save_plugin_runtime_enabled(plugin_name, False)
                    current = self.plugins.get(plugin_name)
                    if current:
                        current.enabled = False
                    return False
                plugin_info = self.plugins[plugin_name]

            # 启用所有监听器
            for listener_id in plugin_info.listener_ids:
                self.event_bus.enable_listener(listener_id)

            self.logger.debug(f"Enabled plugin '{plugin_name}'")
            return True
    
    def disable_plugin(self, plugin_name: str) -> bool:
        """禁用插件"""
        with self._lock:
            if plugin_name not in self.plugins:
                return False

            plugin_info = self.plugins[plugin_name]
            if not self._save_plugin_runtime_enabled(plugin_name, False):
                return False
            plugin_info.enabled = False

            # 禁用所有监听器
            for listener_id in plugin_info.listener_ids:
                self.event_bus.disable_listener(listener_id)

            self.logger.debug(f"Disabled plugin '{plugin_name}'")
            return True

    def _save_plugin_runtime_enabled(self, plugin_name: str, enabled: bool) -> bool:
        """Persist lifecycle state separately from plugin business settings."""
        plugin_info = self.plugins.get(plugin_name)
        if not plugin_info:
            return False
        config_file = Path(plugin_info.path) / "config.json"
        temporary = config_file.with_name(f".{config_file.name}.tmp")
        try:
            with open(config_file, "r", encoding="utf-8") as handle:
                config = json.load(handle)
            runtime = config.get("runtime")
            if not isinstance(runtime, dict):
                runtime = {}
            runtime["enabled"] = bool(enabled)
            config["runtime"] = runtime
            with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(config, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, config_file)
            plugin_info.config = config
            return True
        except Exception as exc:
            self.logger.error("Failed to persist runtime state for '%s': %s", plugin_name, exc)
            try:
                if temporary.exists():
                    temporary.unlink()
            except OSError:
                pass
            return False
    
    def get_plugin_info(self, plugin_name: str) -> Optional[PluginInfo]:
        """获取插件信息"""
        return self.plugins.get(plugin_name)
    
    def list_plugins(self) -> Dict[str, Dict[str, Any]]:
        """列出所有插件"""
        result = {}
        for name, plugin in self.plugins.items():
            # 获取运行中的监听器信息
            listeners = self.get_plugin_listeners(name)
            
            # 推断特性
            features = plugin.config.get("features", [])
            if isinstance(features, list):
                features = list(features) # copy
            else:
                features = []
            
            # 根据 README 规范推断 push 能力
            if "push" not in features:
                if "DAILY_PUSH_TIME" in plugin.config or "ENABLE_DAILY_PUSH" in plugin.config:
                    features.append("push")

            result[name] = {
                "name": plugin.name,
                "display_name": plugin.config.get("display_name", plugin.name),
                "category": plugin.config.get("category", "utility"),
                "version": plugin.version,
                "description": plugin.description,
                "author": plugin.author,
                "enabled": plugin.enabled,
                "loaded": plugin.loaded,
                "listener_count": len(plugin.listener_ids),
                "features": features,
                "manifest_version": plugin.manifest.get("schema_version"),
                "jobs": list(plugin.manifest.get("jobs", [])),
            }
        return result
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {
            "total_plugins": len(self.plugins),
            "loaded_plugins": sum(1 for p in self.plugins.values() if p.loaded),
            "enabled_plugins": sum(1 for p in self.plugins.values() if p.enabled),
            "total_listeners": sum(len(p.listener_ids) for p in self.plugins.values())
        }
        
        # 聚合监听器信息: EventType -> [PluginName, ...]
        listeners_by_type: Dict[str, List[str]] = {}
        
        for plugin in self.plugins.values():
            if not plugin.enabled or not plugin.loaded:
                continue
                
            for listener_id in plugin.listener_ids:
                info = self.event_bus.get_listener_info(listener_id)
                if info and info.get('enabled', True):
                    e_type = info['event_type'].value
                    if e_type not in listeners_by_type:
                        listeners_by_type[e_type] = []
                    
                    if plugin.name not in listeners_by_type[e_type]:
                        listeners_by_type[e_type].append(plugin.name)
        
        stats['listeners_by_type'] = listeners_by_type
        
        # 添加 LLM 统计信息
        try:
            from app.services.llm_manager import get_llm_manager
            llm_manager = get_llm_manager()
            llm_stats = llm_manager.get_stats()
            
            # 汇总所有插件的 LLM 调用统计
            total_calls = 0
            total_tokens = 0
            total_errors = 0
            all_response_times = []
            
            # 使用 session stats（当前运行期间的统计）
            for key, data in llm_stats.get("session", {}).items():
                total_calls += data.get("count", 0)
                total_tokens += data.get("total_tokens", 0)
                total_errors += data.get("error_count", 0)
                all_response_times.extend(data.get("response_times", []))
            
            # 计算平均响应时间
            avg_response_time = 0.0
            if all_response_times:
                avg_response_time = sum(all_response_times) / len(all_response_times)
            
            stats['total_calls'] = total_calls
            stats['total_tokens'] = total_tokens
            stats['error_count'] = total_errors
            stats['avg_response_time'] = avg_response_time
            
        except Exception as e:
            logger.error(f"获取 LLM 统计失败: {e}")
            stats['total_calls'] = 0
            stats['total_tokens'] = 0
            stats['error_count'] = 0
            stats['avg_response_time'] = 0.0
        
        return stats

    def get_all_plugin_names(self) -> List[str]:
        """获取所有已发现插件的名称列表"""
        return list(self.plugins.keys())
    
    def get_plugin_listeners(self, plugin_name: str) -> List[Dict[str, Any]]:
        """获取插件的所有监听器信息"""
        plugin_info = self.plugins.get(plugin_name)
        if not plugin_info:
            return []
        
        listeners = []
        for listener_id in plugin_info.listener_ids:
            listener_info = self.event_bus.get_listener_info(listener_id)
            if listener_info:
                listeners.append(listener_info)

        return listeners
