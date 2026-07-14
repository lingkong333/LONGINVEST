from long_invest.bootstrap.app import create_app
from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging

settings = get_settings()
configure_logging(
    level=settings.log_level,
    queue_capacity=settings.log_queue_capacity,
    log_file=settings.log_file,
)
app = create_app()
