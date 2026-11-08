"""Cloud persistence boundary; local development keeps its existing SQLite store."""
import os


def cloud_enabled():
    return os.environ.get('DATA_BACKEND') == 'cloudbase_rest'


def check_config():
    if os.environ.get('REQUIRE_SERVER_DB') == '1' and not cloud_enabled():
        raise RuntimeError('REQUIRE_SERVER_DB requires DATA_BACKEND=cloudbase_rest')
    if cloud_enabled():
        from .cloudbase import gateway
        gateway()
