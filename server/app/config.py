from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行配置。全部可用环境变量覆盖（compose 里注入）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 视频根目录（容器内路径，只读挂载）
    media_root: str = "/media"
    # 缩略图/转码缓存目录（可写）
    cache_root: str = "/cache"

    database_url: str = "postgresql+asyncpg://kids:kids@db:5432/kids"

    # 用于签发家长登录令牌，务必改掉
    app_secret: str = "change-me-please"
    # 家长端初始密码，首次部署后请改
    parent_password: str = "change-me-please"
    # 家长登录令牌有效期（天）
    token_days: int = 30
    # 设备配对码有效期（秒）
    pair_code_ttl: int = 300

    # 统计按哪个时区切天
    tz: str = "Asia/Shanghai"

    # 媒体文件怎么发：
    #   accel  = 只回 X-Accel-Redirect，由前置 nginx 直出（生产默认，CPU 占用接近零）
    #   direct = FastAPI 自己按 Range 流式返回（本地开发用，没有 nginx 也能播）
    serve_media: str = "accel"


settings = Settings()
