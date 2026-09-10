"""Declarative metadata without opening database or vector-store connections."""

from sqlalchemy.orm import declarative_base

Base = declarative_base()
AppBase = declarative_base()
DebugBase = declarative_base()
