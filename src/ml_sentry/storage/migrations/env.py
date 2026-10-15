from alembic import context

from ml_sentry.storage.models import Base

config = context.config
engine = config.attributes["engine"]

with engine.begin() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
