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

    # ---- 入库流水线 ----
    # 扫描到新视频后自动转音频 + 提取对白（否则只入库元数据）
    auto_ingest: bool = True
    # 转写模型：small.en 在 N100 上约 3-4 倍实时；medium.en 更准但慢 3 倍
    whisper_model: str = "small.en"
    whisper_compute: str = "int8"
    whisper_threads: int = 3
    # 模型缓存目录（要挂持久卷，否则每次重建容器都要重下 480MB）
    model_dir: str = "/models"
    # HuggingFace 镜像；国内直连 huggingface.co 基本不通
    hf_endpoint: str = "https://hf-mirror.com"

    # 媒体文件怎么发：
    #   accel  = 只回 X-Accel-Redirect，由前置 nginx 直出（生产默认，CPU 占用接近零）
    #   direct = FastAPI 自己按 Range 流式返回（本地开发用，没有 nginx 也能播）
    serve_media: str = "accel"


settings = Settings()
