from alembic import context
from sqlalchemy import create_engine
from backend.production.database import meta
from backend.production.security import Settings
config=context.config
url=Settings.load().database_url
def run():
    if context.is_offline_mode():
        context.configure(url=url,target_metadata=meta,literal_binds=True)
        with context.begin_transaction():context.run_migrations()
    else:
        engine=create_engine(url)
        with engine.connect() as conn:
            context.configure(connection=conn,target_metadata=meta,compare_type=True)
            with context.begin_transaction():context.run_migrations()
        engine.dispose()
run()
