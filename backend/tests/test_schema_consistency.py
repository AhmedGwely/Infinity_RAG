"""Guards against drift between ORM models and the hand-written Alembic migration."""
import ast, unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def orm_tables():
    out = {}
    for c in ast.parse((BACKEND / "infinity/models.py").read_text()).body:
        if isinstance(c, ast.ClassDef):
            table, cols = None, set()
            for n in c.body:
                if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "__tablename__":
                    table = n.value.value
                if isinstance(n, ast.AnnAssign) and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "mapped_column":
                    cols.add(n.target.id)
            if table:
                out[table] = cols
    return out


def migration_tables():
    out = {}
    for n in ast.walk(ast.parse((BACKEND / "migrations/versions/0001_initial.py").read_text())):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_table":
            out[n.args[0].value] = {a.args[0].value for a in n.args[1:] if isinstance(a, ast.Call) and getattr(a.func, "attr", "") == "Column"}
    return out


class SchemaTests(unittest.TestCase):
    def test_columns_match(self):
        orm, ddl = orm_tables(), migration_tables()
        self.assertEqual(set(orm), set(ddl))
        for table in orm:
            self.assertEqual(orm[table], ddl[table], table)


if __name__ == "__main__":
    unittest.main()
