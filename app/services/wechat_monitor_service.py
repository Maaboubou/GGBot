#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信掉线监控服务
定期检测微信在线状态，掉线时发送邮件通知
"""

import logging
import threading
import time
from typing import Optional

from .config_service import get_setting
from .email_service import get_email_service

logger = logging.getLogger(__name__)


class WeChatMonitorService:
    """
    微信掉线监控服务
    - 定期检查微信是否在线
    - 掉线时发送邮件通知
    - 恢复时可选发送恢复通知
    """
    
    def __init__(self, wechat_manager=None):
        self.wechat_manager = wechat_manager
        self.email_service = get_email_service()
        self.logger = logging.getLogger(__name__)
        
        # 监控配置
        self.is_monitoring = False
        self.monitor_thread = None
        self.check_interval = 30  # 30秒检查一次
        
        # 防止重复发邮件；连续离线检查达到阈值后才发送掉线通知，避免瞬时超时误报
        self.offline_email_sent = False
        self.offline_failure_count = 0
        self.offline_alert_threshold = 3
        self.last_online_status = None
        self.last_listener_status = None
        self.last_missing_listeners = []
        
        # 从配置获取机器人名称
        self.bot_name = get_setting("WECHAT_BOT_NAME", "微信助手")
        
        self.logger.info(f"🔧 微信掉线监控服务初始化完成 (机器人: {self.bot_name})")
    
    def set_wechat_manager(self, wechat_manager):
        """设置微信管理器实例"""
        self.wechat_manager = wechat_manager
        self.logger.info("微信管理器已设置到监控服务")
    
    def start_monitoring(self) -> bool:
        """启动监控"""
        if self.is_monitoring:
            self.logger.warning("⚠️ 监控服务已在运行")
            return False
            
        if not self.wechat_manager:
            self.logger.error("❌ 微信管理器未设置，无法启动监控")
            return False
        
        self.is_monitoring = True
        self.monitor_thread = threading.Thread(
            target=self._monitor_loop, 
            name="wechat_monitor_service",
            daemon=True
        )
        self.monitor_thread.start()
        self.logger.info("🔍 微信掉线监控已启动")
        return True
    
    def stop_monitoring(self) -> None:
        """停止监控"""
        if not self.is_monitoring:
            return
            
        self.is_monitoring = False
        self.logger.info("🛑 微信掉线监控已停止")
        
        # 等待监控线程结束
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=5)
    
    def _monitor_loop(self) -> None:
        """监控循环"""
        self.logger.info("监控循环已启动")
        consecutive_failures = 0
        max_consecutive_failures = 5
        
        while self.is_monitoring:
            try:
                # 检查微信在线状态
                is_online = self._check_wechat_online()
                
                # 重置连续失败计数
                consecutive_failures = 0
                
                if is_online:
                    listener_status = self._check_listener_status()
                    if listener_status:
                        missing = listener_status.get("missing") or []
                        self.last_missing_listeners = missing
                        current_listener_status = "degraded" if missing else "healthy"
                        if self.last_listener_status != current_listener_status:
                            if missing:
                                self.logger.warning("⚠️ 监听健康检查发现缺失监听: %s", missing)
                            else:
                                self.logger.info("✅ 监听健康检查正常")
                            self.last_listener_status = current_listener_status

                    # 微信在线：清零连续离线计数
                    if self.offline_failure_count:
                        self.logger.info(
                            "✅ 微信在线检查恢复，清零连续失败计数 (%s/%s)",
                            self.offline_failure_count,
                            self.offline_alert_threshold,
                        )
                        self.offline_failure_count = 0

                    if self.offline_email_sent:
                        # 之前发过掉线邮件，现在恢复了，发送恢复邮件
                        self.logger.info("✅ 微信状态恢复正常，发送恢复通知...")
                        if self.email_service.send_recovery_notification(self.bot_name):
                            self.offline_email_sent = False
                        else:
                            self.logger.error("恢复通知邮件发送失败")
                    
                    # 更新状态
                    if self.last_online_status != True:
                        self.logger.info("✅ 微信在线")
                        self.last_online_status = True
                else:
                    # 微信掉线/健康检查超时：连续失败达到阈值后才告警，避免单次 /health 超时误报
                    self.offline_failure_count += 1
                    if self.offline_failure_count < self.offline_alert_threshold:
                        self.logger.warning(
                            "⚠️ 微信在线检查失败 %s/%s，暂不发送掉线通知",
                            self.offline_failure_count,
                            self.offline_alert_threshold,
                        )
                    else:
                        if not self.offline_email_sent:
                            self.logger.warning(
                                "❌ 连续 %s 次检测到微信离线，发送邮件通知...",
                                self.offline_failure_count,
                            )
                            if self.email_service.send_offline_notification(self.bot_name):
                                self.offline_email_sent = True
                            else:
                                self.logger.error("掉线通知邮件发送失败")
                        
                        # 更新状态
                        if self.last_online_status != False:
                            self.logger.warning("⚠️ 微信离线")
                            self.last_online_status = False
                
                # 等待下次检查
                time.sleep(self.check_interval)
                
            except Exception as e:
                consecutive_failures += 1
                self.logger.error(f"监控循环异常 (连续失败 {consecutive_failures}/{max_consecutive_failures}): {e}")
                
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(f"监控服务连续失败 {max_consecutive_failures} 次，延长检查间隔")
                    time.sleep(300)  # 5分钟
                    consecutive_failures = 0  # 重置计数
                else:
                    time.sleep(60)  # 异常时等待1分钟
        
        self.logger.info("监控循环已结束")
    
    def _check_wechat_online(self) -> bool:
        """读取连接监控缓存，避免重复探测微信桥接端口。"""
        try:
            if not self.wechat_manager:
                self.logger.warning("微信管理器未设置")
                return False

            health = self.wechat_manager.get_cached_health()
            is_online = bool(
                health.get("wechat_connected")
                and health.get("wechat_online")
            )
            self.logger.debug(f"微信在线状态: {is_online}")
            return is_online
            
        except Exception as e:
            self.logger.debug(f"检查微信在线状态异常: {e}")
            return False

    def _check_listener_status(self) -> Optional[dict]:
        """检查 wx_bot 监听窗口健康状态。"""
        try:
            if not self.wechat_manager or not hasattr(self.wechat_manager, "get_listener_status"):
                return None

            status = self.wechat_manager.get_listener_status()
            if status.get("status") != "success":
                self.logger.debug("监听健康检查跳过: %s", status.get("message"))
                return status

            missing = status.get("missing") or []
            if missing:
                self.logger.warning(
                    "监听健康检查异常: missing=%s desired=%s actual=%s",
                    missing,
                    status.get("desired"),
                    status.get("actual"),
                )
            return status
        except Exception as e:
            self.logger.debug(f"监听健康检查异常: {e}")
            return None
    
    def get_status(self) -> dict:
        """获取监控服务状态"""
        return {
            "monitoring": self.is_monitoring,
            "bot_name": self.bot_name,
            "check_interval": self.check_interval,
            "offline_email_sent": self.offline_email_sent,
            "offline_failure_count": self.offline_failure_count,
            "offline_alert_threshold": self.offline_alert_threshold,
            "last_online_status": self.last_online_status,
            "last_listener_status": self.last_listener_status,
            "last_missing_listeners": self.last_missing_listeners,
            "wechat_manager_available": self.wechat_manager is not None
        }
    
    def force_check(self) -> dict:
        """强制执行一次检查（用于测试）"""
        if not self.wechat_manager:
            return {"success": False, "message": "微信管理器未设置"}
        
        try:
            is_connected = self.wechat_manager.is_connected()
            is_online = self.wechat_manager.is_online() if is_connected else False
            listener_status = self.wechat_manager.get_listener_status() if is_connected else {}

            return {
                "success": True,
                "wx_bot_connected": is_connected,
                "wechat_online": is_online,
                "listener_status": listener_status,
                "listeners_healthy": not bool((listener_status or {}).get("missing")),
                "timestamp": time.time()
            }
        except Exception as e:
            return {"success": False, "message": str(e)}


# 全局实例
_monitor_service: Optional[WeChatMonitorService] = None

def get_monitor_service() -> WeChatMonitorService:
    """获取监控服务实例（单例模式）"""
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = WeChatMonitorService()
    return _monitor_service

def start_wechat_monitoring(wechat_manager) -> WeChatMonitorService:
    """
    启动微信掉线监控的便捷函数
    
    Args:
        wechat_manager: 微信管理器实例
        
    Returns:
        WeChatMonitorService: 监控服务实例
    """
    monitor_service = get_monitor_service()
    monitor_service.set_wechat_manager(wechat_manager)
    monitor_service.start_monitoring()
    return monitor_service


if __name__ == "__main__":
    # 测试代码
    print("微信掉线监控服务模块")
    print("使用方法:")
    print("from app.services.wechat_monitor_service import start_wechat_monitoring")
    print("monitor = start_wechat_monitoring(wechat_manager)")
