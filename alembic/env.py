from alembic import context
from app import models  # noqa: F401
from app.config import Settings
from app.db import Base, make_engine

config = context.config
url = config.attributes.get("database_url") or Settings().database_url

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(url)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=Base.metadata, render_as_batch=engine.dialect.name == "sqlite"
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
