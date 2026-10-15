from pathlib import Path
import pytest
from app.database import Database


@pytest.mark.asyncio
async def test_sqlite_persists_across_connections(tmp_path: Path):
    database = Database(tmp_path / "app.sqlite3")
    await database.initialize()
    async with database.connect() as connection:
        await connection.execute("CREATE TABLE entries (value TEXT)")
        await connection.execute("INSERT INTO entries VALUES (?)", ("example",))
        await connection.commit()
    async with database.connect() as connection:
        cursor = await connection.execute("SELECT value FROM entries")
        assert (await cursor.fetchone())["value"] == "example"
