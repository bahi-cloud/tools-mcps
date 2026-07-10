"""مصادقة بسيطة عبر Bearer API Key."""
import logging
from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from core.config import settings

logger = logging.getLogger("gateway.security")
bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> str:
    """يتحقق من ترويسة Authorization: Bearer <key>."""
    if credentials is None or credentials.credentials != settings.API_KEY:
        logger.warning("محاولة وصول بمفتاح غير صالح")
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials
