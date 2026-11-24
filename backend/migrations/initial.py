"""Initialize or update the installation from the versioned ORM schema."""
from sqlalchemy import inspect, text

from src.config import engine
from src.models import Base


def upgrade():
    Base.metadata.create_all(engine)
    # Databases created before per-hour baselines existed still need the
    # review-sheet snapshot column; create_all does not alter existing tables.
    columns = {column['name'] for column in inspect(engine).get_columns('energy_reviews')}
    if 'baselines' not in columns:
        with engine.begin() as connection:
            connection.execute(text('ALTER TABLE energy_reviews ADD COLUMN baselines JSON NULL'))


if __name__ == '__main__':
    upgrade()
