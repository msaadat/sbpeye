from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import chromadb
from chromadb.utils import embedding_functions

SQLALCHEMY_DATABASE_URL = "sqlite:///./sbpeye.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name=EMBEDDING_MODEL
)

chroma_client = chromadb.PersistentClient(path="./chroma_db")

try:
    collection = chroma_client.get_or_create_collection(
        name="circulars",
        embedding_function=embedding_fn,
    )
except ValueError:
    collection = chroma_client.get_collection(name="circulars")


def _ensure_columns():
    insp = inspect(engine)
    with engine.begin() as conn:
        if "circulars" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("circulars")}
            new_columns = [
                ("summary", "TEXT"),
                ("tags", "TEXT"),
                ("compliance_checklist", "TEXT"),
                ("status", "VARCHAR(20) DEFAULT 'active'"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE circulars ADD COLUMN {col_name} {col_type}"))

        if "circular_relationships" in insp.get_table_names():
            existing = {c["name"] for c in insp.get_columns("circular_relationships")}
            new_columns = [
                ("target_reference", "TEXT"),
                ("confidence", "FLOAT"),
            ]
            for col_name, col_type in new_columns:
                if col_name not in existing:
                    conn.execute(text(f"ALTER TABLE circular_relationships ADD COLUMN {col_name} {col_type}"))

Base.metadata.create_all(bind=engine)
_ensure_columns()