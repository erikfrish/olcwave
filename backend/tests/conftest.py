"""Non-secret test configuration; all external services are replaced by fixtures."""
import os

for key, value in {
    'RW_ENABLED': 'true',
    'DB_HOST': '127.0.0.1',
    'DB_PORT': '1',
    'POSTGRES_USER': 'test',
    'POSTGRES_PASSWORD': 'test-only-not-a-credential',
    'POSTGRES_DB': 'test',
    'ADMIN_USERNAME': 'test',
    'ADMIN_PASSWORD': 'test-only-not-a-credential',
    'JWT_SECRET_KEY': 'test-only-not-a-credential',
}.items():
    os.environ[key] = value
