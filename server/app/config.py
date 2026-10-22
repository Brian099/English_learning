from pathlib import Path
import secrets

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行配置。全部可用环境变量覆盖（compose 里注入）。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 视频根目录（容器内路径，只读挂载）
    media_root: str = "/media"
    # 缩略图/转码缓存目录（可写）
    cache_root: str = "/cache"
    # 数据库与密钥所在目录（可写）
    data_dir: str = "/data"

    database_url: str = "sqlite+aiosqlite:////data/kids.db"

    # 用于签发家长登录令牌；留空则首次启动自动生成并写入 data_dir/.app_secret
    app_secret: str = ""
    # 家长端初始密码
    parent_password: str = "kids1234"
    # 家长登录令牌有效期（天）
    token_days: int = 30
    # 设备配对码有效期（秒）
    pair_code_ttl: int = 300

    # 统计按哪个时区切天
    tz: str = "Asia/Shanghai"

    # ---- 入库流水线 ----
    # 扫描到新视频后自动转音频 + 提取对白（否则只入库元数据）
    auto_ingest: bool = True
    # 音量统一（EBU R128）：不同来源的素材响度差十几 dB，统一后孩子不用来回调音量
    normalize_audio: bool = True
    # 看模式也走统一音量（会把归一化后的视频写进 cache，额外占空间）
    normalize_video: bool = False
    target_lufs: float = -16.0
    target_tp: float = -1.5
    target_lra: float = 11.0
    # 转写模型：small.en 在 N100 上约 3-4 倍实时；medium.en 更准但慢 3 倍
    whisper_model: str = "small.en"
    whisper_compute: str = "int8"
    whisper_threads: int = 3
    # 模型缓存目录（要挂持久卷，否则每次重建容器都要重下 480MB）
    model_dir: str = "/models"
    # HuggingFace 镜像；国内直连 huggingface.co 基本不通
    hf_endpoint: str = "https://hf-mirror.com"


settings = Settings()


def _resolve_app_secret() -> None:
    """APP_SECRET 没配就自动生成一个，持久化到 data_dir，重启不变。

    这样 compose 里不需要写任何密钥，装完就能用；
    已有的令牌也不会因为重启而失效。
    """
    if settings.app_secret:
        return

    secret_file = Path(settings.data_dir) / ".app_secret"
    try:
        if secret_file.is_file():
            stored = secret_file.read_text(encoding="utf-8").strip()
            if stored:
                settings.app_secret = stored
                return
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        secret_file.write_text(generated, encoding="utf-8")
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        settings.app_secret = generated
    except OSError:
        # 数据目录不可写时退化为进程内随机值（重启后需重新登录）
        settings.app_secret = secrets.token_hex(32)


_resolve_app_secret()
