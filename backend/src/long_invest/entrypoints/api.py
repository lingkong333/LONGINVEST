from long_invest.bootstrap.app import create_app
from long_invest.platform.config.settings import get_settings
from long_invest.platform.logging.configure import configure_logging

configure_logging(level=get_settings().log_level)
app = create_app()
