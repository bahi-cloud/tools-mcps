# composio_service.py - الإصدار النهائي المُصحح
import os
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("composio_session_manager")

class ComposioSessionManager:
    """
    مدير الجلسات والأدوات المتقدم لخوادم MCP وComposio.
    يعالج مشكلة انقطاع الجلسات وتدفق المصادقة عبر Entity/User-ID.
    """

    def __init__(self, api_key: Optional[str] = None, default_user_id: str = "mcp_default_user"):
        self.api_key = api_key or os.getenv("COMPOSIO_API_KEY")
        self.default_user_id = default_user_id
        self.available = False
        self.toolset = None

        if not self.api_key:
            logger.warning("⚠️ COMPOSIO_API_KEY غير متوفر في متغيرات البيئة.")
            return

        try:
            from composio import ComposioToolSet
            self.toolset = ComposioToolSet(api_key=self.api_key)
            self.available = True
            logger.info("✅ Composio initialized successfully")
        except ImportError:
            logger.error("❌ مكتبة composio غير مثبتة. قم بتشغيل: pip install composio-core")
        except Exception as e:
            logger.error(f"❌ فشل تهيئة Composio: {e}")

    def create_session(
        self,
        app_name: str,
        user_id: Optional[str] = None,
        redirect_url: Optional[str] = None,
        wait_for_active: bool = False,
        timeout: int = 60
    ) -> Dict[str, Any]:
        """
        إنشاء جلسة اتصال جديدة وتوفير رابط المصادقة المباشر للوكيل/المستخدم.
        """
        if not self.available or not self.toolset:
            return {
                "success": False,
                "error": "Composio SDK is not properly initialized or missing API Key."
            }

        target_user = user_id or self.default_user_id

        try:
            entity = self.toolset.get_entity(id=target_user)
            connection_request = entity.initiate_connection(
                app_name=app_name,
                redirect_url=redirect_url
            )

            response = {
                "success": True,
                "user_id": target_user,
                "app_name": app_name,
                "connected_account_id": getattr(connection_request, "connectedAccountId", None),
                "redirect_url": getattr(connection_request, "redirectUrl", None),
                "status": getattr(connection_request, "connectionStatus", "INITIATED")
            }

            if wait_for_active:
                logger.info(f"⏳ انتظار تفعيل الجلسة للمستخدم '{target_user}' والتطبيق '{app_name}'...")
                connected_account = connection_request.wait_until_active(timeout=timeout)
                response["status"] = "ACTIVE"
                response["connected_account_id"] = connected_account.id

            return response

        except Exception as e:
            logger.error(f"❌ خطأ أثناء إنشاء الجلسة: {str(e)}")
            return {"success": False, "error": str(e)}

    def execute_tool_action(
        self,
        action_name: str,
        params: Dict[str, Any],
        user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        تنفيذ أداة أو إجراء داخل الجلسة النشطة.
        """
        if not self.available or not self.toolset:
            return {"success": False, "error": "Composio SDK not initialized."}

        target_user = user_id or self.default_user_id

        try:
            entity = self.toolset.get_entity(id=target_user)
            result = entity.execute_action(
                action=action_name,
                params=params
            )
            return {"success": True, "result": result}
        except Exception as e:
            logger.error(f"❌ فشل تنفيذ الأداة {action_name}: {str(e)}")
            return {"success": False, "error": str(e)}

    def list_user_active_sessions(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        استرجاع قائمة الجلسات والتطبيقات النشطة للمستخدم.
        """
        if not self.available or not self.toolset:
            return []

        target_user = user_id or self.default_user_id

        try:
            entity = self.toolset.get_entity(id=target_user)
            connections = entity.get_connections()
            
            return [
                {
                    "connection_id": conn.id,
                    "app_name": conn.appName,
                    "status": conn.status,
                    "created_at": str(conn.createdAt)
                }
                for conn in connections
            ]
        except Exception as e:
            logger.error(f"❌ خطأ أثناء جلب الجلسات للمستخدم {target_user}: {str(e)}")
            return []

# ==========================================
# تجربة وتأكيد إنشاء الجلسة مباشرة
# ==========================================
if __name__ == "__main__":
    manager = ComposioSessionManager()
    
    # تجربة إنشاء جلسة حقيقية
    session_info = manager.create_session(
        app_name="github",
        user_id="user_mcp_session_001",
        wait_for_active=False
    )
    
    print("\n--- نتيجة إنشاء الجلسة ---")
    print(session_info)
    
    if session_info.get("redirect_url"):
        print(f"\nرابط إكمال الجلسة والمصادقة:\n{session_info['redirect_url']}")